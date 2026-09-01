[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PublicKeyPath,

    [string]$ProjectRoot = "",
    [string]$SyncUser = "lecture_sync",
    [string]$PcTailscaleIp = "<PC_TAILSCALE_IP>",
    [string]$PhoneTailscaleIp = "<PHONE_TAILSCALE_IP>",
    [string]$ErrorLogPath = "$env:TEMP\lecture-ai-sftp-setup-error.log"
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

trap {
    $details = $_ | Format-List * -Force | Out-String
    [System.IO.File]::WriteAllText(
        $ErrorLogPath,
        $details,
        (New-Object System.Text.UTF8Encoding($false))
    )
    exit 1
}

$openSshRoot = Join-Path $env:WINDIR "System32\OpenSSH"
$sshdExe = Join-Path $openSshRoot "sshd.exe"
$sshKeygenExe = Join-Path $openSshRoot "ssh-keygen.exe"
$programDataSsh = Join-Path $env:ProgramData "ssh"
$programDataSshLogs = Join-Path $programDataSsh "logs"
$sshdConfig = Join-Path $programDataSsh "sshd_config"
$incomingRoot = Join-Path $ProjectRoot "data\incoming"
$audioRoot = Join-Path $incomingRoot "audio"
$chrootConfigPath = $incomingRoot -replace "\\", "/"

foreach ($requiredPath in @($sshdExe, $sshKeygenExe, $PublicKeyPath, $incomingRoot, $audioRoot)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required path does not exist: $requiredPath"
    }
}

$machineQualifiedUser = "$env:COMPUTERNAME\$SyncUser"
$currentQualifiedUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Get-LocalUser -Name $SyncUser -ErrorAction SilentlyContinue)) {
    $passwordBytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($passwordBytes)
    }
    finally {
        $rng.Dispose()
    }
    $randomPassword = [Convert]::ToBase64String($passwordBytes)
    $securePassword = ConvertTo-SecureString $randomPassword -AsPlainText -Force
    New-LocalUser -Name $SyncUser -Password $securePassword -Description "LectureAI phone SFTP upload account" -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
}
Enable-LocalUser -Name $SyncUser
$usersGroup = Get-LocalGroup -SID "S-1-5-32-545"
$syncUserSid = (Get-LocalUser -Name $SyncUser).SID.Value
$isUsersMember = Get-LocalGroupMember -Group $usersGroup | Where-Object {
    $_.SID.Value -eq $syncUserSid
}
if (-not $isUsersMember) {
    Add-LocalGroupMember -Group $usersGroup -Member $machineQualifiedUser
}
& net.exe user $SyncUser /passwordreq:yes | Out-Null

$userHome = Join-Path "C:\Users" $SyncUser
$userSsh = Join-Path $userHome ".ssh"
$authorizedKeys = Join-Path $userSsh "authorized_keys"
New-Item -ItemType Directory -Path $userSsh -Force | Out-Null
Copy-Item -LiteralPath $PublicKeyPath -Destination $authorizedKeys -Force

& icacls.exe $userHome /inheritance:r | Out-Null
& icacls.exe $userHome /grant:r "${machineQualifiedUser}:(OI)(CI)(F)" "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)" | Out-Null
& icacls.exe $userHome /setowner $machineQualifiedUser /T /C | Out-Null

# The chroot itself must not be writable by the SFTP account. Only /audio is writable.
& icacls.exe $incomingRoot /inheritance:r | Out-Null
& icacls.exe $incomingRoot /setowner "BUILTIN\Administrators" | Out-Null
& icacls.exe $incomingRoot /grant:r "BUILTIN\Administrators:(OI)(CI)(F)" "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" "${currentQualifiedUser}:(OI)(CI)(F)" "${machineQualifiedUser}:(RX)" | Out-Null
& icacls.exe $audioRoot /grant:r "${machineQualifiedUser}:(OI)(CI)(M)" | Out-Null

New-Item -ItemType Directory -Path $programDataSsh -Force | Out-Null
New-Item -ItemType Directory -Path $programDataSshLogs -Force | Out-Null
& icacls.exe $programDataSsh /inheritance:r | Out-Null
& icacls.exe $programDataSsh /grant:r "BUILTIN\Administrators:(OI)(CI)(F)" "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" "NT AUTHORITY\Authenticated Users:(OI)(CI)(RX)" | Out-Null
& icacls.exe $programDataSshLogs /inheritance:r | Out-Null
& icacls.exe $programDataSshLogs /grant:r "BUILTIN\Administrators:(OI)(CI)(F)" "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" "NT AUTHORITY\Authenticated Users:(OI)(CI)(RX)" | Out-Null
& $sshKeygenExe -A | Out-Null
Get-ChildItem -LiteralPath $programDataSsh -File -Filter "ssh_host_*_key" | ForEach-Object {
    & icacls.exe $_.FullName /inheritance:r | Out-Null
    & icacls.exe $_.FullName /setowner "BUILTIN\Administrators" | Out-Null
    & icacls.exe $_.FullName /remove:g $currentQualifiedUser | Out-Null
    & icacls.exe $_.FullName /grant:r "BUILTIN\Administrators:(F)" "NT AUTHORITY\SYSTEM:(F)" | Out-Null
}

$configText = @"
# Managed by scripts/setup_phone_sftp.ps1
Port 22
AddressFamily any
ListenAddress $PcTailscaleIp
PubkeyAuthentication yes
PasswordAuthentication no
PermitEmptyPasswords no
LogLevel VERBOSE
AuthorizedKeysFile C:/Users/$SyncUser/.ssh/authorized_keys
Subsystem sftp internal-sftp
AllowUsers $SyncUser

Match User $SyncUser
    AuthenticationMethods publickey
    ChrootDirectory $chrootConfigPath
    ForceCommand internal-sftp -d /audio
    AllowAgentForwarding no
    AllowTcpForwarding no
    PermitTTY no
    X11Forwarding no
"@
[System.IO.File]::WriteAllText($sshdConfig, $configText, (New-Object System.Text.UTF8Encoding($false)))

& $sshdExe -t -f $sshdConfig
if ($LASTEXITCODE -ne 0) {
    throw "sshd configuration validation failed with exit code $LASTEXITCODE"
}

Set-Service -Name sshd -StartupType Automatic
Stop-Service -Name sshd -Force -ErrorAction SilentlyContinue

Disable-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue | Out-Null
Get-NetFirewallRule -DisplayName "LectureAI SFTP over Tailscale" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule `
    -DisplayName "LectureAI SFTP over Tailscale" `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 22 `
    -LocalAddress $PcTailscaleIp `
    -RemoteAddress $PhoneTailscaleIp `
    -InterfaceAlias "Tailscale" `
    -Profile Any | Out-Null

Start-Service -Name sshd
Write-Output "LectureAI SFTP setup completed."
