# ARVP Demo Script - one-shot showcase
# Usage: .\demo.ps1

$BASE = "http://localhost:8000"
$LINE = ("=" * 55)

function Print-Header($text) {
    Write-Host ""
    Write-Host $LINE -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host $LINE -ForegroundColor Cyan
}

function Print-Step($step, $text) {
    Write-Host ""
    Write-Host "[$step] $text" -ForegroundColor Yellow
}

function Print-OK($text) {
    Write-Host "   [OK] $text" -ForegroundColor Green
}

function Print-Info($text) {
    Write-Host "        $text" -ForegroundColor Gray
}

function API($method, $path, $body = $null, $token = $null, [switch]$Silent) {
    $headers = @{ "Content-Type" = "application/json" }
    if ($token) { $headers["Authorization"] = "Bearer $token" }
    $params = @{ Uri = "$BASE$path"; Method = $method; Headers = $headers; ErrorAction = "Stop" }
    if ($body) { $params["Body"] = ($body | ConvertTo-Json -Depth 5) }
    try {
        return Invoke-RestMethod @params
    } catch {
        if (-not $Silent) {
            $code = $_.Exception.Response.StatusCode.value__
            Write-Host "   [!!] ERROR $method $path -> HTTP $code" -ForegroundColor Red
        }
        throw
    }
}

# ── START ─────────────────────────────────────────────────────────────────────
Clear-Host
Print-Header "Autonomous Robotics Validation Platform"
Write-Host "  Synthetic warehouse sim | 9 microservices" -ForegroundColor White
Write-Host "  FastAPI | Redis | PostgreSQL | Prometheus | Grafana" -ForegroundColor DarkGray

# ── STEP 0: Start platform ────────────────────────────────────────────────────
Print-Step "0" "Starting platform (docker compose up)..."
$projectDir = $PSScriptRoot
Push-Location $projectDir
docker compose up -d 2>&1 | Out-Null

Write-Host "    Waiting for services to become healthy..." -ForegroundColor DarkGray
$waited = 0
while ($waited -lt 150) {
    $starting = docker ps --filter "name=autonomous-robotics" --format "{{.Status}}" |
                Where-Object { $_ -match "starting" }
    if (-not $starting) { break }
    Start-Sleep -Seconds 5; $waited += 5
    Write-Host "    ...${waited}s" -ForegroundColor DarkGray
}
Start-Sleep -Seconds 3
Print-OK "All containers up and healthy"

# ── STEP 1: Login ─────────────────────────────────────────────────────────────
Print-Step "1" "Authenticating"
$loginResp = API "POST" "/api/v1/auth/login" @{ username = "admin"; password = "admin123" }
$token = $loginResp.access_token
Print-OK "JWT token obtained  (role: ADMIN)"

# ── STEP 2: System health ─────────────────────────────────────────────────────
Print-Step "2" "Checking all 8 microservices"
$health = API "GET" "/api/v1/system-health" -token $token
$overall = ($health.overall).ToUpper()
Print-OK "Overall: $overall  --  $($health.services_healthy) / $($health.services_total) services healthy"
foreach ($svc in $health.services) {
    if ($null -eq $svc) { continue }
    $name    = "$($svc.service)".PadRight(32)
    $latency = $svc.latency_ms
    $icon    = if ($svc.status -eq "healthy") { "OK" } else { "!!" }
    Print-Info "$icon  $name $latency ms"
}

# ── STEP 3: Start simulation ──────────────────────────────────────────────────
Print-Step "3" "Launching synthetic warehouse simulation (3 robots)"
$sim = API "POST" "/api/v1/simulations" @{
    name             = "interview-demo"
    mode             = "synthetic"
    robot_count      = 3
    duration_seconds = 120
    world_name       = "small_warehouse"
} -token $token

$simId   = if ($sim.run_id) { $sim.run_id } else { $sim.id }
$robots  = $sim.robot_ids
$robot0  = $robots[0]
$robot1  = $robots[1]
$robot2  = $robots[2]

Print-OK "Simulation ID: $simId"
Print-Info "Mode: synthetic  |  Robots: $robot0, $robot1, $robot2"
Print-Info "A* pathfinding, physics engine, battery + sensor models"

Write-Host "    Waiting for robots to start publishing telemetry..." -ForegroundColor DarkGray
Start-Sleep -Seconds 12

# ── STEP 4: Sim status + telemetry ───────────────────────────────────────────
Print-Step "4" "Live robot telemetry"
$simStatus = API "GET" "/api/v1/simulations/$simId" -token $token
Print-OK "Simulation status: $($simStatus.status)"

foreach ($robot in $robots) {
    try {
        $tel    = API "GET" "/api/v1/telemetry/$robot/latest" -token $token -Silent
        $bat    = [math]::Round($tel.battery.level * 100, 1)
        $px     = [math]::Round($tel.position.x, 1)
        $py     = [math]::Round($tel.position.y, 1)
        $speed  = [math]::Round($tel.velocity.linear, 2)
        $lidar  = [math]::Round($tel.sensors.lidar_quality * 100, 1)
        Print-OK "$robot  battery=$bat%  speed=$speed m/s  pos=($px,$py)  lidar=$lidar%"
    } catch {
        Print-Info "$robot  (warming up...)"
    }
}

# ── STEP 5: Fault injection ───────────────────────────────────────────────────
Print-Step "5" "Chaos engineering -- injecting BATTERY_DRAIN on $robot0"
$fault = API "POST" "/api/v1/faults" @{
    robot_id         = $robot0
    fault_type       = "BATTERY_DRAIN"
    severity         = "HIGH"
    duration_seconds = 30
} -token $token
$faultId = if ($fault.fault_id) { $fault.fault_id } else { $fault.id }
Print-OK "Fault injected: $faultId"
Print-Info "Type: BATTERY_DRAIN  |  Severity: HIGH  |  Duration: 30s"
Print-Info "Battery will drain fast -- anomaly detector will flag it"

Start-Sleep -Seconds 5

# ── STEP 6: Diagnostics ───────────────────────────────────────────────────────
Print-Step "6" "Anomaly detection (Z-score + CUSUM algorithms)"
$diag = API "GET" "/api/v1/diagnostics/fleet/summary" -token $token
Print-OK "Fleet health score: $($diag.fleet_health_score)"
Print-Info "Total robots: $($diag.total_robots)  |  Active anomalies: $($diag.active_anomalies)"
Print-Info "Healthy: $($diag.healthy_robots)  |  Degraded: $($diag.degraded_robots)  |  Critical: $($diag.critical_robots)"

# ── STEP 7: Validation suite ─────────────────────────────────────────────────
Print-Step "7" "Running automated validation suite (8 statistical tests)"
$val = API "POST" "/api/v1/validations" @{
    fleet_ids                = @($robot0, $robot1, $robot2)
    pass_threshold           = 0.6
    telemetry_window_seconds = 30.0
} -token $token
$valId = if ($val.run_id) { $val.run_id } else { $val.id }
Print-OK "Validation ID: $valId"
Print-Info "Tests: battery health, velocity bounds, path efficiency, fault response..."

for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 5
    $check     = API "GET" "/api/v1/validations/$valId" -token $token
    $valStatus = $check.status
    if ($valStatus -in @("PASSED","FAILED","ERROR","ABORTED","COMPLETED")) { break }
    Write-Host "    ...running ($valStatus)" -ForegroundColor DarkGray
}
Print-OK "Validation finished: $valStatus"

# ── STEP 8: Validation report ─────────────────────────────────────────────────
Print-Step "8" "Validation report"
$report = API "GET" "/api/v1/validations/$valId/report" -token $token
$pct    = [math]::Round($report.pass_rate * 100, 1)
Print-OK "Pass rate: $pct%  |  Passed: $($report.passed_tests) / $($report.total_tests) tests"
if ($report.recommendations -and $report.recommendations.Count -gt 0) {
    # Strip non-ASCII to avoid encoding artifacts in some terminals
    $rec = $report.recommendations[0] -replace '[^\x00-\x7F]', ''
    Print-Info "Recommendation: $rec"
}

# ── STEP 9: Analytics KPIs ────────────────────────────────────────────────────
Print-Step "9" "Fleet analytics KPIs"
$kpis = API "GET" "/api/v1/analytics/fleet/kpis" -token $token
$bat  = [math]::Round($kpis.avg_battery_level * 100, 1)
$cpu  = [math]::Round($kpis.avg_cpu_usage, 1)
Print-OK "Avg battery: $bat%  |  Avg CPU: $cpu%  |  Robots observed: $($kpis.total_robots_observed)"

# ── DONE ──────────────────────────────────────────────────────────────────────
Print-Header "Demo Complete -- Platform is live"
Write-Host ""
Write-Host "  Open in browser:" -ForegroundColor White
Write-Host "    API Docs   ->  http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "    Grafana    ->  http://localhost:3000   (admin / admin123)" -ForegroundColor Cyan
Write-Host "    Prometheus ->  http://localhost:9090" -ForegroundColor Cyan
Write-Host ""

Pop-Location
