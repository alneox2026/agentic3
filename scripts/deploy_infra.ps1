param(
    [Parameter(Mandatory = $true)]
    [string]$GatewayImage,

    [Parameter(Mandatory = $true)]
    [string]$WorkerImage,

    [Parameter(Mandatory = $true)]
    [string]$BillingApiImage,

    [string]$ProjectId = "ceo-dev123",
    [string]$Region = "us-central1",
    [string[]]$AllowedOrigins = @("https://ceoappdev.flutterflow.app"),
    [string[]]$BillingApiAllowedOrigins = @("https://ceoappdev.flutterflow.app"),
    [string]$StripeSecretKeySecretVersion = "1",
    [string]$BillingApiCheckoutSuccessUrl = "",
    [string]$BillingApiCheckoutCancelUrl = "",
    [string[]]$AlertNotificationChannels = @(),
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

Require-Command terraform

$terraformDir = Join-Path $PSScriptRoot "..\\infra\\terraform"
$varsPath = Join-Path $terraformDir "terraform.auto.tfvars.json"
$backendConfigPath = Join-Path $terraformDir "backend.hcl"

$payload = @{
    project_id                  = $ProjectId
    region                      = $Region
    gateway_image               = $GatewayImage
    worker_image                = $WorkerImage
    billing_api_image           = $BillingApiImage
    allowed_origins             = $AllowedOrigins
    billing_api_allowed_origins = $BillingApiAllowedOrigins
    billing_api_stripe_secret_key_secret_version = $StripeSecretKeySecretVersion
    alert_notification_channels = $AlertNotificationChannels
}

if (($BillingApiCheckoutSuccessUrl -and -not $BillingApiCheckoutCancelUrl) -or
    ($BillingApiCheckoutCancelUrl -and -not $BillingApiCheckoutSuccessUrl)) {
    throw "Provide both -BillingApiCheckoutSuccessUrl and -BillingApiCheckoutCancelUrl, or neither."
}
if ($BillingApiCheckoutSuccessUrl) {
    $payload.billing_api_checkout_success_url = $BillingApiCheckoutSuccessUrl
    $payload.billing_api_checkout_cancel_url = $BillingApiCheckoutCancelUrl
}

$payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $varsPath -Encoding UTF8

Push-Location $terraformDir
try {
    if (Test-Path -LiteralPath $backendConfigPath) {
        terraform init -backend-config=backend.hcl -reconfigure
    }
    else {
        terraform init
    }
    terraform plan
    if (-not $PlanOnly) {
        terraform apply -auto-approve
    }
}
finally {
    Pop-Location
}
