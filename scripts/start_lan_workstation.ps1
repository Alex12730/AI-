[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$FFmpegDirectory)

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start_local_workstation.ps1') `
    -FFmpegDirectory $FFmpegDirectory `
    -ListenAddress '0.0.0.0'

