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

function Get-RuntimeIdentifier {
    param([string] $Target)
    switch ($Target) {
        "x86_64-pc-windows-msvc" { return "win-x64" }
        "aarch64-pc-windows-msvc" { return "win-arm64" }
        default { throw "Unsupported Windows staging target: $Target" }
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$packageVersion = Normalize-Version $Version
$runtimeIdentifier = Get-RuntimeIdentifier $Target
$distRoot = Join-Path $repoRoot "dist\$Target"
$binDir = Join-Path $distRoot "bin"
$stdlibDir = Join-Path $distRoot "stdlib"
$toolImplDir = Join-Path $distRoot "tools"
$sourceDir = Join-Path $repoRoot "packaging\out\work\windows-stage-$Target\src"
$tools = @("axc", "axbuild", "axpkg", "axls", "axfmt", "axrepl", "axlint", "axup")

Remove-Item -LiteralPath $distRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $sourceDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $binDir, $stdlibDir, $toolImplDir, $sourceDir | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "tools\axon_tool.py") -Destination (Join-Path $toolImplDir "axon_tool.py") -Force

$program = @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;

internal static class Program
{
    private const string Version = "__AXON_VERSION__";
    private const string Target = "__AXON_TARGET__";

    private static int Main(string[] args)
    {
        string tool = Path.GetFileNameWithoutExtension(Environment.ProcessPath ?? AppDomain.CurrentDomain.FriendlyName);
        string? exeDir = Path.GetDirectoryName(Environment.ProcessPath ?? AppDomain.CurrentDomain.BaseDirectory);
        string root = Path.GetFullPath(Path.Combine(exeDir ?? ".", ".."));
        string implementation = Path.Combine(root, "tools", "axon_tool.py");
        if (!File.Exists(implementation))
        {
            Console.Error.WriteLine($"{tool}: missing AXON tool implementation at {implementation}");
            return 70;
        }

        foreach (string python in new[] { "python", "python3", "py" })
        {
            int? status = TryRunPython(python, implementation, tool, args);
            if (status.HasValue)
            {
                return status.Value;
            }
        }

        Console.Error.WriteLine($"{tool}: Python 3 is required to run the AXON toolchain.");
        return 69;
    }

    private static int? TryRunPython(string executable, string implementation, string tool, string[] args)
    {
        try
        {
            List<string> command = new List<string> { implementation, tool };
            command.AddRange(args);
            ProcessStartInfo startInfo = new ProcessStartInfo(executable)
            {
                UseShellExecute = false,
            };
            foreach (string argument in command)
            {
                startInfo.ArgumentList.Add(argument);
            }
            using Process process = Process.Start(startInfo)!;
            process.WaitForExit();
            return process.ExitCode;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return null;
        }
        catch (FileNotFoundException)
        {
            return null;
        }
    }
}
'@

$program = $program.Replace("__AXON_VERSION__", $packageVersion).Replace("__AXON_TARGET__", $Target)

$project = @'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <AssemblyName>axon-tool</AssemblyName>
    <PublishSingleFile>true</PublishSingleFile>
    <SelfContained>true</SelfContained>
  </PropertyGroup>
</Project>
'@

$programPath = Join-Path $sourceDir "Program.cs"
$projectPath = Join-Path $sourceDir "axon-tool.csproj"
Set-Content -LiteralPath $programPath -Value $program -Encoding UTF8
Set-Content -LiteralPath $projectPath -Value $project -Encoding UTF8

dotnet publish $projectPath `
    -c Release `
    -r $runtimeIdentifier `
    -p:DebugType=None `
    -p:DebugSymbols=false `
    -o $binDir | Out-Host

$publishedExe = Join-Path $binDir "axon-tool.exe"
if (-not (Test-Path -LiteralPath $publishedExe -PathType Leaf)) {
    throw "Bootstrap executable was not produced: $publishedExe"
}

foreach ($tool in $tools) {
    Copy-Item -LiteralPath $publishedExe -Destination (Join-Path $binDir "$tool.exe") -Force
}
Remove-Item -LiteralPath $publishedExe -Force

Set-Content -LiteralPath (Join-Path $stdlibDir "README.md") -Value "# AXON Standard Library`n`nThis distribution includes the AXON standard-library contract and toolchain runtime required by the canonical examples.`n" -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination (Join-Path $distRoot "LICENSE")
Copy-Item -LiteralPath (Join-Path $repoRoot "README.md") -Destination (Join-Path $distRoot "README.md")
Copy-Item -LiteralPath (Join-Path $repoRoot "docs") -Destination (Join-Path $distRoot "docs") -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot "examples") -Destination (Join-Path $distRoot "examples") -Recurse

Write-Host "Staged AXON distribution: $distRoot"
