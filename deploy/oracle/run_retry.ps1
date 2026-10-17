# Lance la recherche de VM Oracle sans Cursor. Une deuxieme copie s'arrete toute seule.
$py = Join-Path $env:USERPROFILE "oci-cli-venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "retry_create_a1.py"
$log = Join-Path $PSScriptRoot "retry.log"
& $py -u $script *>> $log
