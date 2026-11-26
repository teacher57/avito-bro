#!/usr/bin/env bash
# avito: установить или обновить BOT BRO и/или HUB BRO на Windows-компьютере клиента с этого Mac.
# Перед этим у клиента один раз: prepare.ps1 (Tailscale, SSH с ключом этого Mac, Node.js) — он и печатает строку
# для запуска этого скрипта.
#
#   ./deploy.sh <пользователь>@<адрес> --bot --hub [--shop-name "Название"] [--dry-run]
#
#   <адрес>       IP в Tailscale (100.x.y.z) или имя компьютера в Tailscale
#   --bot         поставить бота и приложение BOT BRO (Python ставится, только если его нет)
#   --hub         поставить кабинет HUB BRO (ярлык «HUB BRO», значок в трее, https-адрес в Tailscale, расширение Chrome)
#   --shop-name   название магазина для нового магазина (по умолчанию — имя компьютера)
#   --dry-run     только узнать всё о компьютере и показать, что будет сделано; ничего не менять
#
# Повторный запуск = обновление кода: данные магазина (data\store), токены и PIN (bot\manager-app\data), переписка бота
# (bot\live), ключ доступа и уже установленный Python не трогаются. Код берётся из папки, где лежит этот скрипт.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="" BOT=0 HUB=0 DRY=0 SHOP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bot) BOT=1 ;;
    --hub) HUB=1 ;;
    --dry-run) DRY=1 ;;
    --shop-name) SHOP="${2:-}"; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *@*) TARGET="$1" ;;
    *) echo "непонятный параметр: $1 (см. ./deploy.sh --help)"; exit 2 ;;
  esac
  shift
done
[ -n "$TARGET" ] || { echo "нужен <пользователь>@<адрес> (его печатает prepare.ps1 в конце)"; exit 2; }
[ $BOT = 1 ] || [ $HUB = 1 ] || { echo "нужно --bot и/или --hub"; exit 2; }

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR "$TARGET")
say() { printf '\n\033[36m== %s\033[0m\n' "$*"; }
ok() { printf '   \033[32mok:\033[0m %s\n' "$*"; }
plan() { printf '   \033[33mбудет:\033[0m %s\n' "$*"; }

# PowerShell на компьютере клиента: скрипт передаётся в base64 (UTF-16LE) — никаких проблем с кавычками cmd.exe.
# В начале: вывод в UTF-8 (иначе кириллица по SSH превращается в «????») и без индикаторов прогресса (иначе по SSH
# приходит служебный «#< CLIXML …»).
rps() {
  local enc pre='$ProgressPreference = "SilentlyContinue"; [Console]::OutputEncoding = [Text.Encoding]::UTF8; $OutputEncoding = [Text.Encoding]::UTF8;'
  enc=$(printf '%s\n%s' "$pre" "$1" | iconv -f UTF-8 -t UTF-16LE | base64 | tr -d '\n')
  "${SSH[@]}" "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -OutputFormat Text -EncodedCommand $enc" | tr -d '\r'
}

# ---------------------------------------------------------------- 1. что за компьютер
say "Компьютер $TARGET"
"${SSH[@]}" "echo ok" >/dev/null 2>&1 || {
  echo "   SSH не пускает. На компьютере клиента запустите prepare.ps1 (от администратора), проверьте Tailscale на Mac: tailscale status"
  exit 1
}
INFO=$(rps '
$ts = "$env:ProgramFiles\Tailscale\tailscale.exe"
$dns = ""; if (Test-Path $ts) { try { $dns = ((& $ts status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".") } catch {} }
$node = ""; try { $node = (& node -v) } catch {}
$serve = ""; if (Test-Path $ts) { $serve = ((& $ts serve status 2>$null) -join " ") }
"PROFILE=$env:USERPROFILE"
"COMPUTER=$env:COMPUTERNAME"
"DNS=$dns"
"NODE=$node"
"INSTALLED=$(Test-Path "$env:USERPROFILE\claude_trusted\avito")"
"STORE=$(Test-Path "$env:USERPROFILE\claude_trusted\avito\data\store\shop.json")"
"KEY=$(Test-Path "$env:USERPROFILE\claude_trusted\avito\data\server\access-key.txt")"
"PY=$(Test-Path "$env:USERPROFILE\claude_trusted\avito\bot\python")"
"TASK=$([bool](Get-ScheduledTask -TaskName HubBroServer -ErrorAction SilentlyContinue))"
"SERVE=$(if ($serve -match "127.0.0.1:8790") { "yes" } else { "no" })"
# код устройства для лицензии BOT BRO — как bot/bot_license.py device_code(): первые 20 hex
# SHA-256("bot-bro-device:" + MachineGuid в нижнем регистре), заглавными, группами по 5
try {
  $guid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid.Trim().ToLower()
  $hex = -join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes("bot-bro-device:" + $guid)) | ForEach-Object { $_.ToString("x2") })
  $h = $hex.Substring(0, 20).ToUpper()
  "DEVICE=" + (($h.Substring(0, 5), $h.Substring(5, 5), $h.Substring(10, 5), $h.Substring(15, 5)) -join "-")
} catch { "DEVICE=" }
')
val() { printf '%s\n' "$INFO" | sed -n "s/^$1=//p" | head -1; }
PROFILE=$(val PROFILE); COMPUTER=$(val COMPUTER); DNS=$(val DNS); NODEV=$(val NODE); DEVICE=$(val DEVICE)
[ -n "$PROFILE" ] || { echo "   не удалось узнать папку профиля"; printf '%s\n' "$INFO"; exit 1; }
DEST="$PROFILE\\claude_trusted\\avito"
DEST_SCP="${PROFILE//\\//}/claude_trusted/avito"
echo "   профиль:   $PROFILE"
echo "   компьютер: $COMPUTER"
echo "   tailscale: ${DNS:-(не найден)}"
echo "   node:      ${NODEV:-(нет)}"
echo "   код устройства для лицензии: ${DEVICE:-(не удалось узнать)}"
echo "   avito:     $( [ "$(val INSTALLED)" = True ] && echo "уже стоит — обновление" || echo "новая установка" )"
[ -n "$NODEV" ] || { echo "   нет Node.js — запустите prepare.ps1 у клиента"; exit 1; }
if [ $HUB = 1 ] && [ -z "$DNS" ]; then echo "   нет адреса Tailscale — запустите prepare.ps1 у клиента"; exit 1; fi
HUB_URL="https://$DNS"
[ -n "$SHOP" ] || SHOP="$COMPUTER"

# ---------------------------------------------------------------- 2. пакет кода на Mac
say "Пакет кода"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
PKG="$WORK/avito"; mkdir -p "$PKG"
rsync -a --exclude='.git' --exclude='.venv' --exclude='live' --exclude='python' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='manager-app/data' --exclude='.env' --exclude='.env.*' --exclude='node_modules' --exclude='web/dist' \
  --exclude='data/store' --exclude='access-key.txt' --exclude='*.log' --exclude='licensing' --exclude='clients' \
  --exclude='.DS_Store' --exclude='*.lnk' --exclude='*.md' --exclude='hub-bro/data' --include='*/' "$SRC/" "$PKG/"
# документы хаба и формата данных нужны и на месте; инструкции бота и INSTALL.md — только на Mac
for d in hub-bro/docs data/schema data/server data/design; do
  [ -d "$SRC/$d" ] && rsync -a --include='*/' --include='*.md' --exclude='*' "$SRC/$d/" "$PKG/$d/"
done
rm -f "$PKG/deploy.sh"
[ $BOT = 1 ] || rm -rf "$PKG/bot"
[ $HUB = 1 ] || rm -rf "$PKG/hub-bro" "$PKG/open_hub.vbs" "$PKG/open_hub.ps1"
if [ $HUB = 1 ]; then
  node "$PKG/hub-bro/extension/set-hub.mjs" "$HUB_URL" "$PKG/hub-bro/extension" >/dev/null
  ok "расширение: прописан адрес $HUB_URL"
  if [ $DRY = 1 ]; then
    plan "собрать кабинет (npm run build)"
  else
    ( cd "$SRC/hub-bro/web" && { [ -d node_modules ] || npm ci --no-audit --no-fund >/dev/null; } && npm run build >/dev/null )
    cp -R "$SRC/hub-bro/web/dist" "$PKG/hub-bro/web/dist"
    ok "кабинет собран"
  fi
  CLIENT_EXT="$SRC/clients/${COMPUTER:-client}/extension"
fi
( cd "$WORK" && COPYFILE_DISABLE=1 tar -czf avito.tgz avito )
ok "пакет: $(du -h "$WORK/avito.tgz" | cut -f1) ($( [ $BOT = 1 ] && echo "бот " )$( [ $HUB = 1 ] && echo "хаб " )+ сервер данных)"

if [ $DRY = 1 ]; then
  say "Что будет сделано (--dry-run, ничего не менялось)"
  plan "скопировать код в $DEST (данные, токены, переписка не трогаются)"
  [ "$(val KEY)" = True ] && ok "ключ доступа уже есть" || plan "создать ключ доступа data\\server\\access-key.txt"
  if [ $HUB = 1 ]; then
    [ "$(val STORE)" = True ] && ok "данные магазина уже есть" || plan "новый магазин «$SHOP», наличие со склада"
    [ "$(val TASK)" = True ] && plan "удалить старый автозапуск HubBroServer (SYSTEM): хаб теперь от имени пользователя, по ярлыку"
    plan "ярлык «HUB BRO» на рабочем столе и в $DEST (поднять хаб, вернуть значок, открыть кабинет)"
    [ "$(val SERVE)" = yes ] && ok "https-адрес $HUB_URL уже настроен" || plan "https-адрес $HUB_URL (tailscale serve)"
    plan "значок HUB BRO в трее (задача HubBroTray, при входе в Windows)"
    plan "расширение для клиента: $CLIENT_EXT"
  fi
  if [ $BOT = 1 ]; then
    [ "$(val PY)" = True ] && ok "Python для бота уже стоит" || plan "поставить Python в папку бота"
    plan "bot\\setup.cmd: окружения (только недостающее), ярлык BOT BRO, автозапуск AvitoBot"
  fi
  exit 0
fi

# ---------------------------------------------------------------- 3. копирование
say "Копирование на компьютер клиента"
if [ "$(val INSTALLED)" = True ]; then
  # обновление: остановить хаб и сервер данных (их запустит заново ярлык «HUB BRO» или «BOT BRO»)
  rps "
schtasks /End /TN HubBroServer 2>\$null | Out-Null
Get-CimInstance Win32_Process -Filter \"name='node.exe'\" | Where-Object { \$_.CommandLine -match 'server\\\\server\\.mjs' } |
  ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }
" >/dev/null || true
fi
"${SSH[@]}" "if not exist \"$DEST\" mkdir \"$DEST\"" >/dev/null
scp -q -o BatchMode=yes -o LogLevel=ERROR "$WORK/avito.tgz" "$TARGET:$DEST_SCP/avito.tgz"
"${SSH[@]}" "cd /d \"$DEST\\..\" && tar -xzf avito\\avito.tgz && del avito\\avito.tgz" >/dev/null
ok "код в $DEST"

# ---------------------------------------------------------------- 4. сервер данных
say "Сервер данных"
SHOP_JSON=$(node -e 'console.log(JSON.stringify(process.argv[1]))' "$SHOP")
rps "
Set-Location '$DEST'
if (-not (Test-Path 'data\\server\\access-key.txt')) {
  node -e \"require('fs').writeFileSync('data/server/access-key.txt', require('crypto').randomBytes(24).toString('base64url'))\"
  'ключ доступа создан'
} else { 'ключ доступа уже есть' }
if ($HUB -eq 1 -and -not (Test-Path 'data\\store\\shop.json')) {
  New-Item -ItemType Directory -Force data\\store | Out-Null
  \$shop = @{ format='shop'; version=1; name=$SHOP_JSON; stockSource='warehouse'; cutoffs=@{ weekday='19:00'; weekend='18:00' };
             serviceOrder=@('yandex','avito','cdek','post','fivepost','dpd') } | ConvertTo-Json -Depth 3
  [IO.File]::WriteAllText(\"\$PWD\\data\\store\\shop.json\", \$shop)
  'новый магазин: наличие со склада'
}
" | sed 's/^/   /'

# ---------------------------------------------------------------- 5. хаб
if [ $HUB = 1 ]; then
  say "HUB BRO"
  rps "
# хаб работает от имени пользователя и стартует по ярлыку «HUB BRO»; старый автозапуск от SYSTEM (прошлые версии) — убрать
if (Get-ScheduledTask -TaskName HubBroServer -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName HubBroServer -Confirm:\$false
  'старый автозапуск HubBroServer (SYSTEM) удалён'
}
# значок для ярлыка: зелёный кружок с белой H, как в трее
Add-Type -AssemblyName System.Drawing
\$ico = '$DEST\\hub-bro\\server\\hub.ico'
\$bmp = New-Object System.Drawing.Bitmap 64, 64
\$g = [System.Drawing.Graphics]::FromImage(\$bmp); \$g.SmoothingMode = 'AntiAlias'; \$g.TextRenderingHint = 'AntiAliasGridFit'
\$g.FillEllipse((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(46, 125, 50))), 2, 2, 60, 60)
\$font = New-Object System.Drawing.Font('Segoe UI', 30, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
\$fmt = New-Object System.Drawing.StringFormat; \$fmt.Alignment = 'Center'; \$fmt.LineAlignment = 'Center'
\$g.DrawString('H', \$font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF(0, 2, 64, 64)), \$fmt); \$g.Dispose()
\$fs = [IO.File]::Create(\$ico); [System.Drawing.Icon]::FromHandle(\$bmp.GetHicon()).Save(\$fs); \$fs.Close()
# ярлык «HUB BRO»: на рабочем столе и в папке avito
foreach (\$dir in @([Environment]::GetFolderPath('Desktop'), '$DEST')) {
  \$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path \$dir 'HUB BRO.lnk'))
  \$lnk.TargetPath = \"\$env:WINDIR\\System32\\wscript.exe\"
  \$lnk.Arguments = '\"$DEST\\open_hub.vbs\"'
  \$lnk.WorkingDirectory = '$DEST'
  \$lnk.IconLocation = \"\$ico,0\"
  \$lnk.Description = 'HUB BRO: start the hub and open the cabinet'
  \$lnk.Save()
}
'ярлык «HUB BRO»: на рабочем столе и в папке avito'
\$ts = \"\$env:ProgramFiles\\Tailscale\\tailscale.exe\"
if (((& \$ts serve status 2>\$null) -join ' ') -match '127.0.0.1:8790') { 'https-адрес уже настроен' }
else { & \$ts serve --bg 8790 | Out-Null; 'https-адрес настроен' }
# значок HUB BRO в трее: при входе пользователя в Windows (задача HubBroTray), сразу запускаем
\$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
\$act = New-ScheduledTaskAction -Execute \"\$env:WINDIR\\System32\\wscript.exe\" -Argument '\"$DEST\\hub-bro\\server\\hub-tray.vbs\"'
\$trg = New-ScheduledTaskTrigger -AtLogOn -User \$me
\$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
\$prn = New-ScheduledTaskPrincipal -UserId \$me -LogonType Interactive
Register-ScheduledTask -TaskName HubBroTray -Action \$act -Trigger \$trg -Settings \$set -Principal \$prn -Force | Out-Null
Start-ScheduledTask -TaskName HubBroTray
'значок HUB BRO в трее: при входе в Windows (задача HubBroTray), запущен'
" | sed 's/^/   /'
  mkdir -p "$(dirname "$CLIENT_EXT")"; rm -rf "$CLIENT_EXT"; cp -R "$PKG/hub-bro/extension" "$CLIENT_EXT"
  ok "расширение для клиента: $CLIENT_EXT (и у клиента: $DEST\\hub-bro\\extension)"
  echo "   хаб сам не стартует: его поднимает ярлык «HUB BRO» у клиента (по SSH запущенный хаб умер бы вместе с сеансом)"
fi

# ---------------------------------------------------------------- 6. бот
if [ $BOT = 1 ]; then
  say "BOT BRO (Python и окружения — только если их нет)"
  "${SSH[@]}" "powershell -NoProfile -ExecutionPolicy Bypass -File \"$DEST\\bot\\setup.ps1\"" 2>&1 | tr -d '\r' | sed 's/^/   /' || {
    echo "   setup.ps1 завершился с ошибкой — см. текст выше"; exit 1; }
fi

# ---------------------------------------------------------------- итог
say "Готово. Осталось руками"
n=1
if [ $HUB = 1 ]; then
  echo "   $n. У клиента: ярлык «HUB BRO» на рабочем столе → хаб запустится и откроется кабинет"; n=$((n+1))
  echo "   $n. Кабинет: $HUB_URL — ключ доступа: ssh $TARGET \"type $DEST\\data\\server\\access-key.txt\""; n=$((n+1))
  echo "   $n. Расширение Chrome: папку $CLIENT_EXT → chrome://extensions → «Загрузить распакованное»"; n=$((n+1))
fi
if [ $BOT = 1 ]; then
  echo "   $n. У клиента: ярлык «BOT BRO» → первая настройка (PIN и токены) → прислать код устройства"; n=$((n+1))
  echo "   $n. Лицензия, код устройства ${DEVICE:-<из BOT BRO>}:"
  echo "        node licensing/license.mjs new \"Клиент\" ${DEVICE:-<код>} [срок] → show → ключ клиенту; выложить avito/licenses.json"
fi
