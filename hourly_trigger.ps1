# 每小时踢一次 GitHub 发布（schedule 被静默限制时的兜底）
# 由 Windows 计划任务 VoidLensPriceTrigger 调用；删任务：schtasks /Delete /TN VoidLensPriceTrigger /F
$repo = 'D:/Backup/Documents/deepseek-harness/default-workspace/voidlens-price-table'
$key  = 'D:/Backup/Documents/deepseek-harness/default-workspace/.ssh_deploy/id_ed25519'
Set-Location $repo
$env:GIT_SSH_COMMAND = 'ssh -i "' + $key + '" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
git pull --rebase --autostash origin main | Out-Null
if ($LASTEXITCODE -ne 0) {
    Start-Sleep -Seconds 20
    git pull --rebase --autostash origin main | Out-Null
}
Set-Content -Path 'TRIGGER.txt' -Value ('trigger ' + $stamp) -Encoding UTF8
git add TRIGGER.txt
git commit -q -m ('chore: hourly trigger ' + $stamp)
if ($LASTEXITCODE -ne 0) { exit 0 }
git push origin main
exit $LASTEXITCODE
