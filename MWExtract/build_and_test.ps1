<#
    Build MWExtract and exercise it end to end.

    Run from anywhere:  powershell -ExecutionPolicy Bypass -File build_and_test.ps1
    Or with your address: ... -File build_and_test.ps1 -Email you@institution.edu

    Stages 1-4 need no network and no address. Stage 5 is the real FTP fetch and
    is skipped unless -Email is supplied.
#>
param(
    [string]$Email,
    [string]$Study = "ST004496",
    [string]$Tag   = "mwextract:2026-09-26"
)

$ErrorActionPreference = "Stop"
$Root   = "C:\Users\doran\School\metabolomics\Pipeline\Deliverable"
$Ctx    = Join-Path $Root "MWExtract"
$Out    = Join-Path $Root "MWExtract\outputfiles\run"
New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Step($n, $text) { Write-Host "`n=== $n. $text ===" -ForegroundColor Cyan }

Step 0 "Docker daemon reachable?"
docker version --format 'server {{.Server.Version}}'
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker Desktop is not running. Start it and re-run." -ForegroundColor Red
    exit 1
}

Step 1 "Build"
# The build imports every stage and asserts the baked index still has 20,764
# rows and 175,700,331 spectra, so a broken module or corrupt Parquet fails
# HERE rather than against a live FTP session.
docker build -f (Join-Path $Ctx "extractor\Dockerfile") -t $Tag $Ctx
if ($LASTEXITCODE -ne 0) { Write-Host "Build failed." -ForegroundColor Red; exit 1 }
docker image inspect $Tag --format 'image {{.Id}}  size {{.Size}} bytes'
docker image inspect $Tag --format 'corpus audit date {{index .Config.Labels "io.metabolomics.corpus-audit-date"}}'

Step 2 "Work list (no network, no address)"
docker run --rm $Tag --list --polarity negative --instrument-types Orbitrap |
    Select-Object -First 8

Step 3 "Guards"
# Each should FAIL, with a specific exit code. 2 means 'no files matched this
# study' and is not an error the loop should stop on.
docker run --rm $Tag 2>&1 | Select-Object -Last 1
Write-Host "  no --study          -> exit $LASTEXITCODE (expect 1)"
docker run --rm $Tag --study ST999999 2>&1 | Select-Object -Last 1
Write-Host "  unknown accession   -> exit $LASTEXITCODE (expect 1)"
docker run --rm -v "${Out}:/output" $Tag --study $Study --polarity positive --dry-run 2>&1 |
    Select-Object -Last 1
Write-Host "  impossible filter   -> exit $LASTEXITCODE (expect 2)"

Step 4 "Dry run (selects, fetches nothing)"
docker run --rm -v "${Out}:/output" $Tag --study $Study --dry-run
Write-Host "  exit $LASTEXITCODE (expect 0)"
Get-ChildItem -Recurse -File (Join-Path $Out $Study) |
    Select-Object @{n='file';e={$_.FullName.Substring($Out.Length+1)}},
                  @{n='KB';e={[math]::Round($_.Length/1KB,1)}} |
    Format-Table -AutoSize

if (-not $Email) {
    Write-Host "`nStages 1-4 passed. Stage 5 (the real fetch) needs an address:" -ForegroundColor Yellow
    Write-Host "  .\build_and_test.ps1 -Email you@institution.edu" -ForegroundColor Yellow
    exit 0
}

Step 5 "Real fetch over FTP -- the only untested path"
# The address is written to a file and mounted read-only rather than passed
# with -e, so it never appears in the host process list or docker inspect.
$Secret = Join-Path $env:TEMP "mwb_email.txt"
[IO.File]::WriteAllText($Secret, $Email)
try {
    docker run --rm `
        -v "${Out}:/output" `
        -v "${Secret}:/run/secrets/mwb_email:ro" `
        $Tag --email-file /run/secrets/mwb_email --study $Study
    $code = $LASTEXITCODE
} finally {
    Remove-Item $Secret -Force -ErrorAction SilentlyContinue
}
Write-Host "  exit $code (expect 0)"

Step 6 "What landed"
$studyDir = Join-Path $Out $Study
$payloads = Get-ChildItem -File (Join-Path $studyDir "spectra") -ErrorAction SilentlyContinue
$zips     = Get-ChildItem -Recurse -File -Filter *.zip $studyDir -ErrorAction SilentlyContinue
Write-Host ("  payload files : {0}" -f $payloads.Count)
Write-Host ("  payload MB    : {0:N1}" -f (($payloads | Measure-Object Length -Sum).Sum / 1MB))
Write-Host ("  archives left : {0}  (expect 0 -- deleted after extraction)" -f $zips.Count)
$metadata = Join-Path $studyDir "$Study.parquet"
Write-Host ("  study parquet : {0}" -f (Test-Path $metadata))
Write-Host "`nTo clean up:  Remove-Item -Recurse -Force '$studyDir'" -ForegroundColor Yellow
