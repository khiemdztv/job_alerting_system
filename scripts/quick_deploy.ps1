param([switch]$Deploy, [string]$Profile = 'vieclambot')
$ErrorActionPreference = "Stop"
$env:AWS_PROFILE = $Profile
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExe = Join-Path $projectRoot 'dist/python/python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
}
& $pythonExe (Join-Path $PSScriptRoot 'build_reviewed_package.py')
if ($LASTEXITCODE -ne 0) { throw 'Package validation failed' }
$packagePath = Join-Path $projectRoot 'dist/deployment-reviewed.zip'
if (!$Deploy) {
    Write-Host "Package ready for review: $packagePath"
    Write-Host 'Use -Deploy after reviewing changes and signing in to AWS.'
    exit 0
}
$functions = @('vieclambot-scraper', 'vieclambot-etl', 'vieclambot-matcher', 'vieclambot-webhook')
# Validate all runtimes before changing any function. Never hide a missing matcher.
foreach ($functionName in $functions) {
    $configJson = aws lambda get-function-configuration --function-name $functionName --region ap-southeast-1 --query '{Runtime:Runtime,Architectures:Architectures}' --output json
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect $functionName" }
    $config = $configJson | ConvertFrom-Json
    if ($config.Runtime -ne 'python3.12' -or $config.Architectures[0] -ne 'x86_64') {
        throw "Runtime mismatch for $functionName; use the full deployment script"
    }
}
foreach ($functionName in $functions) {
    aws lambda update-function-code --function-name $functionName --zip-file "fileb://$packagePath" --region ap-southeast-1 --query '{FunctionName:FunctionName,LastUpdateStatus:LastUpdateStatus}' --output json
    if ($LASTEXITCODE -ne 0) { throw "Deployment failed for $functionName" }
    aws lambda wait function-updated --function-name $functionName --region ap-southeast-1
    if ($LASTEXITCODE -ne 0) { throw "Update did not complete for $functionName" }
}
Write-Host 'All four functions updated. Run scripts/diagnose.py to check schedules, queue and Telegram.'
