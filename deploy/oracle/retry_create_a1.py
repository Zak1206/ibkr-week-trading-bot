"""
Retente la creation de la VM Always Free Ampere A1 jusqu'a ce qu'Oracle ait de la place.

Prerequis : ~/.oci/config (cle API de la console) et ~/.ssh/oracle_bot.pub.
Lancement : python deploy/oracle/retry_create_a1.py
Arret propre : Ctrl+C. Notifie sur Telegram si TELEGRAM_BOT_TOKEN/CHAT_ID sont dans .env.
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

import oci

INSTANCE_NAME = "trading-bot"
SHAPE = "VM.Standard.A1.Flex"
# 2 OCPU / 12 GB d'abord, 1 OCPU / 6 GB en secours. Un seul format par essai.
SIZES = [(2, 12), (1, 6)]
# Un seul appel par tour : deux appels rapproches declenchent le 429 d'Oracle.
SLEEP_RANGE_SEC = (120, 240)
SSH_PUB = Path.home() / ".ssh" / "oracle_bot.pub"
REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_FH = None


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def telegram(text: str) -> bool:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return False
    vals = {}
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    token, chat = vals.get("TELEGRAM_BOT_TOKEN"), vals.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        import requests

        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text},
            timeout=10,
        )
        return resp.ok
    except Exception:
        return False


def existing_a1(compute, compartment: str):
    for inst in oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment_id=compartment
    ).data:
        if inst.shape == SHAPE and inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return inst
    return None


def ensure_public_subnet(network, compartment: str) -> str:
    for subnet in oci.pagination.list_call_get_all_results(
        network.list_subnets, compartment_id=compartment
    ).data:
        if subnet.lifecycle_state == "AVAILABLE" and not subnet.prohibit_public_ip_on_vnic:
            log(f"Subnet public existant : {subnet.display_name}")
            return subnet.id

    log("Aucun subnet public : creation VCN + internet gateway + subnet")
    vcn = network.create_vcn(
        oci.core.models.CreateVcnDetails(
            compartment_id=compartment, cidr_blocks=["10.0.0.0/16"], display_name="trading-bot-vcn"
        )
    ).data
    oci.wait_until(network, network.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE")
    igw = network.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment, vcn_id=vcn.id, is_enabled=True, display_name="trading-bot-igw"
        )
    ).data
    oci.wait_until(network, network.get_internet_gateway(igw.id), "lifecycle_state", "AVAILABLE")
    network.update_route_table(
        vcn.default_route_table_id,
        oci.core.models.UpdateRouteTableDetails(
            route_rules=[
                oci.core.models.RouteRule(
                    destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id
                )
            ]
        ),
    )
    subnet = network.create_subnet(
        oci.core.models.CreateSubnetDetails(
            compartment_id=compartment,
            vcn_id=vcn.id,
            cidr_block="10.0.0.0/24",
            display_name="trading-bot-subnet",
            prohibit_public_ip_on_vnic=False,
        )
    ).data
    oci.wait_until(network, network.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE")
    return subnet.id


def ubuntu_image(compute, compartment: str) -> str:
    images = compute.list_images(
        compartment_id=compartment,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    images = [i for i in images if "Minimal" not in (i.display_name or "")]
    if not images:
        sys.exit("Image Ubuntu 24.04 aarch64 introuvable")
    log(f"Image : {images[0].display_name}")
    return images[0].id


def public_ip(compute, network, compartment: str, instance_id: str) -> str:
    for att in compute.list_vnic_attachments(compartment_id=compartment, instance_id=instance_id).data:
        vnic = network.get_vnic(att.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return "?"


def _single_instance() -> bool:
    """False si une autre copie tourne deja. Le fichier reste ouvert tant que le process vit."""
    global _LOCK_FH
    path = Path(__file__).resolve().parent / "retry_create_a1.lock"
    _LOCK_FH = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(_LOCK_FH.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def main() -> None:
    if "--test-telegram" not in sys.argv and not _single_instance():
        log("Deja en cours, cette copie s'arrete.")
        return
    config = oci.config.from_file()
    oci.config.validate_config(config)
    compartment = config["tenancy"]
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)
    identity = oci.identity.IdentityClient(config)

    if not SSH_PUB.exists():
        sys.exit(f"Cle SSH publique absente : {SSH_PUB}")
    ssh_key = SSH_PUB.read_text(encoding="utf-8").strip()

    if "--test-telegram" in sys.argv:
        ok = telegram("Test : le script Oracle te previendra ici quand la VM sera prete.")
        log("Telegram OK" if ok else "Telegram ECHEC (token/chat_id dans .env ?)")
        return

    found = existing_a1(compute, compartment)
    if found:
        log(f"Une VM A1 existe deja ({found.display_name}, {found.lifecycle_state}) : rien a faire.")
        return

    ads = [ad.name for ad in identity.list_availability_domains(compartment_id=compartment).data]
    subnet_id = ensure_public_subnet(network, compartment)
    image_id = ubuntu_image(compute, compartment)
    log(f"Availability domains : {', '.join(ads)}")

    attempt = 0
    while True:
        for ad in ads:
            for ocpus, mem in [SIZES[attempt % len(SIZES)]]:
                attempt += 1
                details = oci.core.models.LaunchInstanceDetails(
                    compartment_id=compartment,
                    availability_domain=ad,
                    display_name=INSTANCE_NAME,
                    shape=SHAPE,
                    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                        ocpus=ocpus, memory_in_gbs=mem
                    ),
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        image_id=image_id,
                        # 50 Go : le defaut, compte dans les 200 Go de disque gratuits.
                        boot_volume_size_in_gbs=50,
                    ),
                    create_vnic_details=oci.core.models.CreateVnicDetails(
                        subnet_id=subnet_id, assign_public_ip=True
                    ),
                    metadata={"ssh_authorized_keys": ssh_key},
                )
                try:
                    inst = compute.launch_instance(details).data
                except oci.exceptions.ServiceError as exc:
                    msg = (exc.message or "").lower()
                    if "capacity" in msg or exc.status == 500:
                        log(f"#{attempt} {ocpus} OCPU/{mem} GB : pas de place")
                        continue
                    if exc.status == 429:
                        log(f"#{attempt} trop de requetes, pause 5 min")
                        time.sleep(300)
                        continue
                    if "limit" in msg:
                        log(f"#{attempt} {ocpus} OCPU/{mem} GB : hors limite free ({exc.message})")
                        continue
                    raise
                except Exception as exc:
                    # Reseau coupe (sortie de veille, Wi-Fi) : la requete a pu passer cote Oracle.
                    log(f"#{attempt} erreur reseau ({type(exc).__name__}), pause 1 min")
                    time.sleep(60)
                    try:
                        found = existing_a1(compute, compartment)
                    except Exception:
                        continue
                    if found:
                        log(f"VM creee malgre l'erreur reseau ({found.display_name}) : verifier la console.")
                        telegram(f"VM Oracle creee ({found.display_name}), IP a lire dans la console.")
                        return
                    continue
                log(f"Instance creee ({ocpus} OCPU/{mem} GB), demarrage...")
                oci.wait_until(
                    compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING", max_wait_seconds=900
                )
                ip = public_ip(compute, network, compartment, inst.id)
                text = f"VM Oracle prete : {ocpus} OCPU / {mem} GB, IP {ip}"
                log(text)
                log(f"Connexion : ssh -i {SSH_PUB.with_suffix('')} ubuntu@{ip}")
                telegram(text)
                return
        wait = random.randint(*SLEEP_RANGE_SEC)
        log(f"Nouvel essai dans {wait} s")
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Arret demande.")
