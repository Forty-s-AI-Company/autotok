#!/usr/bin/env bash
# 輪替 TikTok Client Secret：把新機密同時寫進本機 .env 與 Vercel（Production、Sensitive）。
# 機密只經過你的終端機輸入與這支腳本，不會顯示在畫面上，也不會進 git。
#
# 步驟：
#   1. 先到 TikTok 開發者後台，重新產生 Client Secret 並複製。
#   2. 在專案資料夾執行：bash scripts/rotate_tiktok_secret.sh
#   3. 貼上新的機密後按 Enter（輸入時不會顯示）。
#   4. 腳本跑完後，重啟 AutoTok；Vercel 需要重新部署一次才會用新的機密。
set -euo pipefail
cd "$(dirname "$0")/.."

export VERCEL_ORG_ID="team_lkOZjk8cXSBz8VvgnrZ6cDbF"
export VERCEL_PROJECT_ID="prj_C2cYpe45hefbsk4HaZcP9prcxOyS"

[ -f .env ] || { echo "找不到 .env，請在專案資料夾執行。"; exit 1; }

read -rsp "貼上新的 TikTok Client Secret（輸入時不會顯示）： " NEW_SECRET
echo
[ -n "$NEW_SECRET" ] || { echo "沒有輸入任何內容，已取消。"; exit 1; }

OLD_SECRET="$(grep '^TIKTOK_CLIENT_SECRET=' .env | cut -d= -f2- || true)"
if [ "$NEW_SECRET" = "$OLD_SECRET" ]; then
  echo "這和目前使用的機密相同，代表還沒有在 TikTok 後台重新產生。已取消。"
  exit 1
fi

# 1) 本機 .env（用 awk 的 ENVIRON，機密裡有特殊字元也不會出錯）
NEW="$NEW_SECRET" awk 'BEGIN{FS=OFS="="} /^TIKTOK_CLIENT_SECRET=/{print "TIKTOK_CLIENT_SECRET=" ENVIRON["NEW"]; found=1; next} {print} END{if(!found) print "TIKTOK_CLIENT_SECRET=" ENVIRON["NEW"]}' .env > .env.tmp
mv .env.tmp .env
echo "已更新本機 .env"

# 2) Vercel（Production、Sensitive；printf '%s' 避免多餘的換行）
printf '%s' "$NEW_SECRET" | vercel env add TIKTOK_CLIENT_SECRET production --sensitive --force >/dev/null
echo "已更新 Vercel 的 TIKTOK_CLIENT_SECRET（Production）"

echo
echo "完成。接下來："
echo "  - 重啟 AutoTok（啟動控制台.bat）讓它讀到新的 .env"
echo "  - Vercel 需重新部署一次才會使用新的機密"
