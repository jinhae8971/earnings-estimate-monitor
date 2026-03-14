# ─────────────────────────────────────────────────────────────────────────────
# NVDA/MU 이익추정치 모니터 — GitHub 자동 배포 스크립트
# ─────────────────────────────────────────────────────────────────────────────
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding            = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$GH_USER    = "jinhae8971"
$GH_REPO    = "earnings-estimate-monitor"
# 아래 값을 실제 값으로 교체하거나, 환경변수로 주입하세요
$GH_TOKEN   = $env:GH_TOKEN   # 예: $env:GH_TOKEN = "ghp_..."
$TG_TOKEN   = $env:TG_TOKEN   # 예: $env:TG_TOKEN = "BOT_TOKEN"
$TG_CHAT    = $env:TG_CHAT    # 예: $env:TG_CHAT  = "CHAT_ID"
$REMOTE_URL = "https://$GH_TOKEN@github.com/$GH_USER/$GH_REPO.git"

$API_HDR = @{
    "Authorization" = "token $GH_TOKEN"
    "Accept"        = "application/vnd.github+json"
    "User-Agent"    = "EarningsMonitorDeploy"
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
Write-Host "배포 디렉터리: $ScriptDir" -ForegroundColor Cyan

# ── [1] Git 초기화 ────────────────────────────────────────────────────────────
git config --global --add safe.directory ($ScriptDir -replace '\\','/') 2>$null
if (-not (Test-Path ".git")) { git init | Out-Null }

$prev = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
git remote remove origin 2>$null | Out-Null
$ErrorActionPreference = $prev

git remote add origin $REMOTE_URL
git config user.name  $GH_USER
git config user.email "jinhae8971@gmail.com"

# .gitignore
@"
config.json
*.pyc
__pycache__/
.env
*.log
"@ | Set-Content -Encoding UTF8 ".gitignore"

Write-Host "[1] Git 초기화 완료" -ForegroundColor Green

# ── [2] GitHub 레포 생성 ──────────────────────────────────────────────────────
try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO" `
        -Headers $API_HDR | Out-Null
    Write-Host "[2] 레포 이미 존재" -ForegroundColor Green
} catch {
    try {
        Invoke-RestMethod -Method Post `
            -Uri "https://api.github.com/user/repos" `
            -Headers $API_HDR `
            -Body (@{name=$GH_REPO; private=$true; auto_init=$false} | ConvertTo-Json) `
            -ContentType "application/json" | Out-Null
        Write-Host "[2] 레포 생성 완료 (private)" -ForegroundColor Green
        Start-Sleep -Seconds 2
    } catch {
        Write-Host "[2] 레포를 수동으로 생성하세요: https://github.com/new (이름: $GH_REPO)" -ForegroundColor Red
        Read-Host "레포 생성 후 Enter"
    }
}

# ── [3] data/ 폴더 초기화 ─────────────────────────────────────────────────────
if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }
# 초기 히스토리 파일 (빈 구조)
if (-not (Test-Path "data\estimates_history.json")) {
    @'
{"NVDA": [], "MU": []}
'@ | Set-Content -Encoding UTF8 "data\estimates_history.json"
    Write-Host "[3] data/estimates_history.json 초기화" -ForegroundColor Green
}

# ── [4] Commit & Push ─────────────────────────────────────────────────────────
$ErrorActionPreference = "SilentlyContinue"
git add .
git commit -m "feat: initial deploy earnings-estimate-monitor" 2>$null
if ($LASTEXITCODE -ne 0) { git commit --allow-empty -m "chore: update" 2>$null }
git branch -M main
git push -u origin main --force 2>$null
$pushCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($pushCode -ne 0) {
    Write-Host "[4] PUSH 실패 — 토큰 'repo' 스코프 확인: https://github.com/settings/tokens" -ForegroundColor Red
    exit 1
}
Write-Host "[4] Push 완료" -ForegroundColor Green

# ── [5] Secrets 등록 ──────────────────────────────────────────────────────────
$secrets = @{
    TELEGRAM_TOKEN   = $TG_TOKEN
    TELEGRAM_CHAT_ID = $TG_CHAT
}

if (Get-Command gh -ErrorAction SilentlyContinue) {
    $env:GH_TOKEN = $GH_TOKEN
    foreach ($s in $secrets.GetEnumerator()) {
        gh secret set $s.Key --body $s.Value --repo "$GH_USER/$GH_REPO" 2>$null
    }
    Write-Host "[5] Secrets 등록 완료 (gh CLI)" -ForegroundColor Green
} else {
    Write-Host "[5] Secrets 수동 등록:" -ForegroundColor Yellow
    Write-Host "    URL: https://github.com/$GH_USER/$GH_REPO/settings/secrets/actions" -ForegroundColor White
    foreach ($s in $secrets.GetEnumerator()) {
        Write-Host "    $($s.Key) = $($s.Value)" -ForegroundColor Cyan
    }
    Read-Host "등록 완료 후 Enter"
}

# ── [6] 워크플로우 즉시 트리거 (수동 실행 테스트) ───────────────────────────
Write-Host "[6] 워크플로우 즉시 트리거 중..." -ForegroundColor White
Start-Sleep -Seconds 3
try {
    Invoke-RestMethod -Method Post `
        -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO/actions/workflows/earnings_monitor.yml/dispatches" `
        -Headers $API_HDR `
        -Body '{"ref":"main"}' `
        -ContentType "application/json" | Out-Null
    Write-Host "[6] 트리거 완료! 약 2~3분 후 텔레그램 확인" -ForegroundColor Green
} catch {
    Write-Host "[6] 수동 트리거: https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White
}

Write-Host ""
Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  배포 완료!" -ForegroundColor Green
Write-Host "  레포:    https://github.com/$GH_USER/$GH_REPO" -ForegroundColor White
Write-Host "  스케줄:  평일 08:00 KST 텔레그램 자동 발송" -ForegroundColor White
Write-Host "  Actions: https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White
Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
