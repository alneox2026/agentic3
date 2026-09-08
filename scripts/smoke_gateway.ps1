param(
    [Parameter(Mandatory = $true)]
    [string]$ServiceUrl,

    [Parameter(Mandatory = $true)]
    [string]$AuthToken,

    [Parameter(Mandatory = $true)]
    [string]$Message,

    [string]$AgentId = "antigravity_agent",

    [string]$ThreadId,
    [string]$SessionId,
    [switch]$Stream
)

$ErrorActionPreference = "Stop"
$normalizedUrl = $ServiceUrl.TrimEnd("/")
$body = @{
    message = $Message
}

if ($ThreadId) {
    $body.thread_id = $ThreadId
}

if ($SessionId) {
    $body.session_id = $SessionId
}

$jsonBody = $body | ConvertTo-Json -Depth 6 -Compress

if ($Stream) {
    $tempFile = Join-Path $env:TEMP "ceoagent-stream-body.json"
    Set-Content -LiteralPath $tempFile -Value $jsonBody -Encoding UTF8
    try {
        & curl.exe -N -sS `
            -X POST `
            "$normalizedUrl/v1/agents/$AgentId/chat/stream" `
            -H "Authorization: Bearer $AuthToken" `
            -H "Content-Type: application/json" `
            --data-binary "@$tempFile"
    }
    finally {
        Remove-Item -LiteralPath $tempFile -ErrorAction SilentlyContinue
    }
    exit $LASTEXITCODE
}

$headers = @{
    Authorization = "Bearer $AuthToken"
    "Content-Type" = "application/json"
}

Invoke-RestMethod `
    -Method POST `
    -Uri "$normalizedUrl/v1/agents/$AgentId/chat" `
    -Headers $headers `
    -Body $jsonBody
