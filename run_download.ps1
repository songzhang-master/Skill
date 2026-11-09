[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Request,
    [switch]$Preview,
    [string]$Python
)

# Prefer the selected Python, then look in already registered local Conda environments.
$originalPath = $env:Path
$originalProj = $env:PROJ_LIB
$originalProjData = $env:PROJ_DATA
$originalGdal = $env:GDAL_DATA
$originalUtf8 = $env:PYTHONUTF8

function Set-ImageryRuntime([string]$exe) {
    $env:Path = $originalPath
    $env:PROJ_LIB = $originalProj
    $env:PROJ_DATA = $originalProjData
    $env:GDAL_DATA = $originalGdal
    $library = Join-Path (Split-Path -Parent $exe) 'Library'
    if (Test-Path -LiteralPath "$library\bin") { $env:Path = "$library\bin;$originalPath" }
    if (Test-Path -LiteralPath "$library\share\proj\proj.db") {
        $env:PROJ_LIB = "$library\share\proj"
        $env:PROJ_DATA = "$library\share\proj"
    }
    if (Test-Path -LiteralPath "$library\share\gdal") { $env:GDAL_DATA = "$library\share\gdal" }
}

try {
    $env:PYTHONUTF8 = '1'
    $file = (Resolve-Path -LiteralPath $Request -ErrorAction Stop).ProviderPath
    if ($Python) {
        $exe = (Get-Command -Name $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    } else {
        $candidates = @(Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
            Where-Object Source -NotLike '*\WindowsApps\*' | ForEach-Object Source)
        $registry = Join-Path $env:USERPROFILE '.conda\environments.txt'
        if (Test-Path -LiteralPath $registry) {
            $candidates += @(Get-Content -LiteralPath $registry | Where-Object {
                $_ -and (Test-Path -LiteralPath (Join-Path $_ 'Lib\site-packages\osgeo'))
            } | ForEach-Object { Join-Path $_ 'python.exe' })
        }
        $exe = $null
        $fallback = $null
        foreach ($candidate in ($candidates | Select-Object -Unique)) {
            if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
            Set-ImageryRuntime $candidate
            & $candidate -c 'import sys; sys.exit(sys.version_info < (3,8))' 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            if (-not $fallback) { $fallback = $candidate }
            & $candidate -c 'from osgeo import gdal, osr' 2>$null
            if ($LASTEXITCODE -eq 0) { $exe = $candidate; break }
        }
        if (-not $exe) { $exe = $fallback }
        if (-not $exe) { throw '找不到 Python 3.8+。请使用 -Python 指定解释器路径。' }
    }
    Set-ImageryRuntime $exe
    $entry = Join-Path $PSScriptRoot 'download_imagery.py'
    & $exe $entry --request $file --dry-run
    $status = $LASTEXITCODE
    if ($status -eq 0 -and -not $Preview) {
        & $exe $entry --request $file
        $status = $LASTEXITCODE
    }
} finally {
    $env:Path = $originalPath
    $env:PROJ_LIB = $originalProj
    $env:PROJ_DATA = $originalProjData
    $env:GDAL_DATA = $originalGdal
    $env:PYTHONUTF8 = $originalUtf8
}
exit $status
