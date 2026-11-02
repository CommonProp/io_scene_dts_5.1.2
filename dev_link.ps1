$ErrorActionPreference = "Stop"

$target = $PSScriptRoot
$addons = Join-Path $env:APPDATA "Blender Foundation\Blender\5.1\scripts\addons"
$link = Join-Path $addons "io_scene_dts"

$extension = Join-Path $env:APPDATA "Blender Foundation\Blender\5.1\extensions\user_default\io_scene_dts"
$zipCopy = Join-Path $addons "io_scene_dts-master"

if (-not (Test-Path -LiteralPath $addons)) {
    New-Item -ItemType Directory -Path $addons | Out-Null
}

function Write-OtherCopies {
    if (Test-Path -LiteralPath $extension) {
        Write-Host "Disable the existing extension first:"
        Write-Host "  $extension"
    }
    if (Test-Path -LiteralPath $zipCopy) {
        Write-Host "Disable the zip addon first:"
        Write-Host "  $zipCopy"
    }
}

if (Test-Path -LiteralPath $link) {
    $item = Get-Item -LiteralPath $link
    $isJunction = $item.Attributes.ToString().Contains("ReparsePoint")
    if ($isJunction) {
        $dest = @($item.Target)[0]
        if ($dest -eq $target) {
            Write-Host "Junction already points at this folder:"
            Write-Host "  $link"
            Write-OtherCopies
            exit 0
        }
    }
    Write-Host "Refusing: $link already exists and is not a junction to this folder."
    Write-Host "Remove that addon (or that leftover install-from-disk copy) first."
    Write-OtherCopies
    exit 1
}

New-Item -ItemType Junction -Path $link -Target $target | Out-Null
Write-Host "Linked $link"
Write-Host "  -> $target"
Write-Host "Enable 'Torque DTS format' once in Blender 5.1 Preferences > Add-ons."
Write-Host "After edits, use F3 > Reload Scripts. Do not Install from Disk."
Write-OtherCopies
