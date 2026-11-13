# SPDX-License-Identifier: GPL-3.0-or-later
# Packs the player zip (release model b: no game code or data in it; the player's PC lifts and links pkp.exe from their own dump).
# Everything built from this repo is taken from the given build outputs and the checkout; the unchanged third-party payload (Zig, embedded
# Python, FSR 3 / Streamline DLLs, astcdec, fonts, input icons, licence texts) comes from a base package (the previous release zip; release.yml BASE_REPO / BASE_RELEASE).
# ponytail: base zip for the third-party payload; fetch each piece from its own source when one of them needs a new version.
#   pwsh game-build\scripts\make-release.ps1 -Version 0.2.0 -Base PokopiaRecomp-0.1.1-win64.zip -RtDir game-build\build\rtdll `
#        -Launcher launcher\src-tauri\target\release\pokopia-launcher.exe -Harness work\rel\harness -Out work\rel
param(
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string]$Base,       # previous player zip (third-party payload)
    [Parameter(Mandatory)][string]$RtDir,      # folder with pkp_rt.dll + pkp_rt.lib (game-build, PKP_RT_DLL=ON PKP_RELEASE=ON)
    [Parameter(Mandatory)][string]$Launcher,   # pokopia-launcher.exe (launcher, npm run tauri build)
    [Parameter(Mandatory)][string]$Harness,    # self-contained spirv_harness publish folder
    [Parameter(Mandatory)][string]$Out
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$name = "PokopiaRecomp-$Version"
$stage = Join-Path $Out 'stage'
$pkg = Join-Path $stage $name
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null

# base payload, its top folder renamed
[IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path $Base).Path, $stage)
$top = Get-ChildItem $stage -Directory | Select-Object -First 1
Rename-Item $top.FullName $name

function Put([string]$from, [string]$to) {
    $dst = Join-Path $pkg $to
    New-Item -ItemType Directory -Force (Split-Path -Parent $dst) | Out-Null
    Copy-Item -Force $from $dst
}
function Mirror([string]$from, [string]$to, [string[]]$exclude = @()) {
    $dst = Join-Path $pkg $to
    if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
    New-Item -ItemType Directory -Force $dst | Out-Null
    $src = (Resolve-Path $from).Path
    Get-ChildItem $src -Recurse -File | Where-Object {
        $rel = $_.FullName.Substring($src.Length + 1)
        -not ($exclude | Where-Object { $rel -like $_ })
    } | ForEach-Object {
        $t = Join-Path $dst $_.FullName.Substring($src.Length + 1)
        New-Item -ItemType Directory -Force (Split-Path -Parent $t) | Out-Null
        Copy-Item $_.FullName $t
    }
}

# built here
Put (Join-Path $RtDir 'pkp_rt.dll') 'pkp_rt.dll'
Put (Join-Path $RtDir 'pkp_rt.lib') 'tools\build\sdk\pkp_rt.lib'
# libusb 1.0.30, unchanged official build (runtime/cmake/gfx_deps.cmake copies it next to pkp_rt.dll): SDL loads it by name for
# controllers it reaches over USB, so it sits next to pkp.exe, ahead of any other libusb-1.0.dll on PATH.
Put (Join-Path $RtDir 'libusb-1.0.dll') 'libusb-1.0.dll'
Put "$root\runtime\third_party\libusb\COPYING" 'licenses\LICENSE.libusb.LGPL-2.1.txt'
Put $Launcher 'PokopiaLauncher.exe'
Mirror $Harness 'tools\spirv_harness' @('*.pdb')
# from the checkout
Put "$root\game-build\src\pkp_rt_api.h" 'tools\build\sdk\pkp_rt_api.h'
Put "$root\game-build\src\pkp_stub.cpp" 'tools\build\sdk\pkp_stub.cpp'
Put "$root\game-build\res\pkp.manifest" 'tools\build\sdk\pkp.manifest'
Put "$root\game-build\res\pkp.rc" 'tools\build\sdk\pkp.rc'
foreach ($f in 'pkp_build.py', 'dump_hashes.json', 'fetch_dlss.py') { Put "$root\game-build\release_b\$f" "tools\build\$f" }
(Get-Content -Raw "$root\game-build\release_b\README.txt").Replace('@VERSION@', $Version) | Set-Content -NoNewline (Join-Path $pkg 'README.txt')
Put "$root\game-build\release_b\BUNDLED_TOOLS.txt" 'licenses\BUNDLED_TOOLS.txt'
# the overlay's font (runtime src/gfx/overlay.cpp looks in fonts\ next to pkp.exe): not in the base payload, so from the checkout
Put "$root\runtime\assets\fonts\MPLUSRounded1c-Medium.ttf" 'fonts\MPLUSRounded1c-Medium.ttf'
Put "$root\runtime\assets\fonts\LICENSE-MPLUSRounded1c.txt" 'licenses\LICENSE-MPLUSRounded1c.txt'
Put "$root\launcher\release\Play.bat" 'Play.bat'
Put "$root\LICENSE" 'LICENSE.txt'
Put "$root\CHANGELOG.md" 'CHANGELOG.txt'
Put "$root\tools\shaders\ampere\extract_corpus.py" 'tools\shaders\extract_corpus.py'
Mirror "$root\runtime\assets\prompts" 'assets\prompts'   # button prompt icon pack prompt_icons.pak (Kenney CC0, licence in the folder)
Put "$root\recomp\LICENSE" 'tools\build\recomp\LICENSE'
Mirror "$root\recomp\nxrecomp" 'tools\build\recomp\nxrecomp' @('*__pycache__*', '*.pyc')
Mirror "$root\recomp\include\nxrecomp" 'tools\build\recomp\include\nxrecomp'
foreach ($f in 'CMakeLists.txt', 'config.py', 'main.sites') { Put "$root\recomp\games\pkp\$f" "tools\build\recomp\games\pkp\$f" }
# what the package ships under tools\build\recomp: pkp_build.py stamps a build by these files only, so a file an older release
# left in an updated install (an update never removes one) does not change the stamp
$recompDir = Join-Path $pkg 'tools\build\recomp'
$shipped = Get-ChildItem $recompDir -Recurse -File | ForEach-Object { $_.FullName.Substring($recompDir.Length + 1).Replace('\', '/') } | Sort-Object
[IO.File]::WriteAllText((Join-Path $pkg 'tools\build\recomp.files'), (($shipped -join "`n") + "`n"))

# never ship anything from a game dump, a shader cache built from the game, saves or keys
$bad = Get-ChildItem $pkg -Recurse -Force | Where-Object {
    $_.Name -match '\.(nso|nro|nca|nsp|xci|nup|spv|keys|bin)$' -or $_.Name -in 'exefs', 'romfs', 'shader_cache', 'save', 'saves', 'main', 'pkp.exe', 'pkp.ini' -or
    $_.FullName -match '\\updates\\'
}
if ($bad) { $bad | ForEach-Object { Write-Error "game-derived or private file in the package: $($_.FullName)" -ErrorAction Continue }; throw 'package check failed' }

$zip = Join-Path $Out "$name-win64.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
[IO.Compression.ZipFile]::CreateFromDirectory($pkg, $zip, [IO.Compression.CompressionLevel]::Optimal, $true)
Write-Host "wrote $zip ($([math]::Round((Get-Item $zip).Length / 1MB)) MiB)"
