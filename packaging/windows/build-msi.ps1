param(
    [Parameter(Mandatory = $true)]
    [string] $Version,

    [Parameter(Mandatory = $true)]
    [string] $Target
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Normalize-Version {
    param([string] $Raw)
    $normalized = $Raw.Trim()
    if ($normalized.StartsWith("v")) {
        $normalized = $normalized.Substring(1)
    }
    if ($normalized -notmatch '^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$') {
        throw "Invalid AXON version '$Raw'. Expected semantic version such as 1.0.0."
    }
    return "$($Matches[1]).$($Matches[2]).$($Matches[3])"
}

function Get-FileId {
    param([string] $Path)
    $hash = [System.Security.Cryptography.SHA256]::HashData([System.Text.Encoding]::UTF8.GetBytes($Path))
    $hex = -join ($hash[0..7] | ForEach-Object { $_.ToString("x2") })
    return "F_$hex"
}

function Get-ComponentId {
    param([string] $Path)
    $hash = [System.Security.Cryptography.SHA256]::HashData([System.Text.Encoding]::UTF8.GetBytes($Path))
    $hex = -join ($hash[8..15] | ForEach-Object { $_.ToString("x2") })
    return "C_$hex"
}

function Escape-XmlAttribute {
    param([string] $Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

function Assert-RequiredToolchain {
    param([string] $DistRoot)
    $tools = @("axc", "axbuild", "axpkg", "axls", "axfmt", "axrepl", "axlint", "axup")
    foreach ($tool in $tools) {
        $path = Join-Path $DistRoot "bin\$tool.exe"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required AXON tool is missing: $path"
        }
    }
    $implementation = Join-Path $DistRoot "tools\axon_tool.py"
    if (-not (Test-Path -LiteralPath $implementation -PathType Leaf)) {
        throw "Required AXON implementation is missing: $implementation"
    }
}

function Test-RequiredToolchain {
    param([string] $DistRoot)
    $tools = @("axc", "axbuild", "axpkg", "axls", "axfmt", "axrepl", "axlint", "axup")
    foreach ($tool in $tools) {
        $path = Join-Path $DistRoot "bin\$tool.exe"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $false
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $DistRoot "tools\axon_tool.py") -PathType Leaf)) {
        return $false
    }
    return $true
}

function Get-WixArchitecture {
    param([string] $Target)
    switch ($Target) {
        "x86_64-pc-windows-msvc" { return "x64" }
        "aarch64-pc-windows-msvc" { return "arm64" }
        default { throw "Unsupported Windows packaging target: $Target" }
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$packageVersion = Normalize-Version $Version
$wixArchitecture = Get-WixArchitecture $Target
$distRoot = Join-Path $repoRoot "dist\$Target"
$outDir = Join-Path $repoRoot "packaging\out\windows"
$workDir = Join-Path $repoRoot "packaging\out\work\windows-$Target"
$stageDir = Join-Path $workDir "AXON"

if (-not (Test-Path -LiteralPath $distRoot -PathType Container) -or -not (Test-RequiredToolchain $distRoot)) {
    $stageScript = Join-Path $repoRoot "packaging\windows\stage-dist.ps1"
    if (-not (Test-Path -LiteralPath $stageScript -PathType Leaf)) {
        throw "Staged distribution not found or incomplete, and staging script is missing: $stageScript"
    }

    Write-Host "Staged distribution not found or incomplete. Creating $distRoot..."
    & $stageScript -Version $Version -Target $Target
}

Assert-RequiredToolchain $distRoot

Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stageDir, $outDir | Out-Null
Copy-Item -Path (Join-Path $distRoot "*") -Destination $stageDir -Recurse -Force

if (-not (Test-Path -LiteralPath (Join-Path $stageDir "LICENSE") -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination (Join-Path $stageDir "LICENSE")
}
if (-not (Test-Path -LiteralPath (Join-Path $stageDir "docs") -PathType Container)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs") -Destination (Join-Path $stageDir "docs") -Recurse
}

$wxsPath = Join-Path $workDir "axon.wxs"
$msiPath = Join-Path $outDir "axon-$packageVersion-$Target.msi"
$upgradeCode = "A3C7A50E-AE8D-47C7-9A9F-5E00E76036A1"

$files = Get-ChildItem -LiteralPath $stageDir -Recurse -File | Sort-Object FullName
$components = New-Object System.Collections.Generic.List[string]

foreach ($file in $files) {
    $relative = [System.IO.Path]::GetRelativePath($stageDir, $file.FullName).Replace("\", "/")
    $componentId = Get-ComponentId $relative
    $fileId = Get-FileId $relative
    $source = Escape-XmlAttribute $file.FullName
    $name = Escape-XmlAttribute $file.Name
    $directory = Split-Path $relative -Parent
    $directoryAttr = " Directory=`"INSTALLFOLDER`""
    if ($directory) {
        $directoryId = "D_" + (($directory -replace '[^A-Za-z0-9_]', '_').Trim("_"))
        $directoryAttr = " Directory=`"$directoryId`""
    }
    $components.Add("      <Component Id=`"$componentId`" Guid=`"*`"$directoryAttr><File Id=`"$fileId`" Source=`"$source`" Name=`"$name`" KeyPath=`"yes`" /></Component>")
}

function Add-Directories {
    param([string] $Root, [string] $BaseId, [int] $Depth)
    $result = New-Object System.Collections.Generic.List[string]
    $dirs = Get-ChildItem -LiteralPath $Root -Directory | Sort-Object Name
    foreach ($dir in $dirs) {
        $relative = [System.IO.Path]::GetRelativePath($stageDir, $dir.FullName).Replace("\", "/")
        $id = "D_" + (($relative -replace '[^A-Za-z0-9_]', '_').Trim("_"))
        $indent = " " * (10 + ($Depth * 2))
        $dirName = Escape-XmlAttribute $dir.Name
        $result.Add("$indent<Directory Id=`"$id`" Name=`"$dirName`">")
        $childDirectories = @(Add-Directories $dir.FullName $id ($Depth + 1))
        if ($childDirectories.Count -gt 0) {
            $result.AddRange([string[]] $childDirectories)
        }
        $result.Add("$indent</Directory>")
    }
    return $result
}

$directoryXml = Add-Directories $stageDir "INSTALLFOLDER" 0
$componentXml = $components -join "`n"
$directoryXmlText = $directoryXml -join "`n"
$componentRefs = ($components | ForEach-Object {
    if ($_ -match 'Component Id="([^"]+)"') {
        "      <ComponentRef Id=""$($Matches[1])"" />"
    }
}) -join "`n"

@"
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package
      Name="AXON"
      Manufacturer="Marcel R."
      Version="$packageVersion"
      UpgradeCode="$upgradeCode"
      Scope="perMachine">
    <MajorUpgrade DowngradeErrorMessage="A newer version of AXON is already installed." />
    <MediaTemplate EmbedCab="yes" />

    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLFOLDER" Name="AXON">
$directoryXmlText
      </Directory>
    </StandardDirectory>

$componentXml

    <Component Id="PathEnvironment" Directory="INSTALLFOLDER" Guid="*">
      <RegistryValue Root="HKLM" Key="Software\AXON" Name="InstallDir" Value="[INSTALLFOLDER]" Type="string" KeyPath="yes" />
      <Environment Id="AddAxonToPath" Name="PATH" Value="[INSTALLFOLDER]bin" Permanent="no" Part="last" Action="set" System="yes" />
    </Component>

    <Feature Id="Main" Title="AXON Toolchain" Level="1">
      <ComponentGroupRef Id="PayloadComponents" />
      <ComponentRef Id="PathEnvironment" />
    </Feature>

    <ComponentGroup Id="PayloadComponents">
$componentRefs
    </ComponentGroup>
  </Package>
</Wix>
"@ | Set-Content -LiteralPath $wxsPath -Encoding UTF8

wix build $wxsPath -arch $wixArchitecture -acceptEula wix7 -out $msiPath

if (-not (Test-Path -LiteralPath $msiPath -PathType Leaf)) {
    throw "MSI was not produced: $msiPath"
}

Write-Host "Built $msiPath"
