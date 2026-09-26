param(
    [Parameter(Mandatory)][string]$Executable,
    [Parameter(Mandatory)][ValidateSet('win32-x64', 'win32-arm64')][string]$Target,
    [Parameter(Mandatory)][string]$ExpectedSubject,
    [Parameter(Mandatory)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ExpectedSubject) -or $ExpectedSubject -match '[\r\n]') {
    throw 'The protected approved Windows certificate subject is missing or invalid.'
}
$verified = Get-AuthenticodeSignature -LiteralPath $Executable
if ($verified.Status -ne 'Valid' -or $verified.SignatureType -ne 'Authenticode' -or
    $null -eq $verified.SignerCertificate -or
    -not [string]::Equals($verified.SignerCertificate.Subject, $ExpectedSubject, [StringComparison]::Ordinal)) {
    throw 'Windows runtime must have a valid embedded Authenticode signature from the approved publisher.'
}
if ($null -eq $verified.TimeStamperCertificate) {
    throw 'Windows runtime must have a verified timestamp before its short-lived certificate expires.'
}

# Azure rotates leaf certificates. Bind the actual certificate for this target into
# the signed release manifest instead of pinning a long-lived certificate thumbprint.
$evidence = @{
    schemaVersion = 2
    target = $Target
    executable = [IO.Path]::GetFileName($Executable)
    executableSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Executable).Hash.ToLowerInvariant()
    kind = 'authenticode'
    status = 'verified'
    signerThumbprint = $verified.SignerCertificate.Thumbprint.ToLowerInvariant()
    signerIdentity = 'sha256:' + $verified.SignerCertificate.GetCertHashString(
        [Security.Cryptography.HashAlgorithmName]::SHA256
    ).ToLowerInvariant()
    timestampSignerIdentity = 'sha256:' + $verified.TimeStamperCertificate.GetCertHashString(
        [Security.Cryptography.HashAlgorithmName]::SHA256
    ).ToLowerInvariant()
}
[IO.File]::WriteAllText($OutputPath, ($evidence | ConvertTo-Json) + "`n", [Text.UTF8Encoding]::new($false))
