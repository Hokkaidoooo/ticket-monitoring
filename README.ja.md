# ticket-monitoring · 航空券の価格監視

[简体中文](README.md) · [English](README_EN.md) · [Español](README.es.md) · 日本語

[dengmeiluan/ticket-monitoring](https://github.com/dengmeiluan/ticket-monitoring) をフォークした、国内線・国際線の運賃を自分の端末で監視するツールです。路線と出発日を指定して価格カレンダーや推移を確認し、目標価格に達したら DingTalk で通知を受け取れます。[上流のオンラインデモ](https://dengmeiluan.github.io/ticket-monitoring/site/)はサンプルデータを使用します。Python を入れずに使う場合は [上流の配布パッケージ](https://github.com/dengmeiluan/ticket-monitoring/releases) を参照してください。

## このフォークをソースから起動する

Python 3.11 以降が必要です。セットアップスクリプトが仮想環境、依存パッケージ、Playwright 用の Chromium を準備します。

**Windows（PowerShell）**

```powershell
git clone https://github.com/Hokkaidoooo/ticket-monitoring.git
cd ticket-monitoring
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\python.exe -m core.setup_check
.\.venv\Scripts\python.exe main.py
```

**macOS / Linux**

```bash
git clone https://github.com/Hokkaidoooo/ticket-monitoring.git
cd ticket-monitoring
bash setup.sh
source .venv/bin/activate
python -m core.setup_check
python main.py
```

## 最初の路線を登録する

1. `http://127.0.0.1:8765` または端末に表示された URL を開きます。設定画面（`配置`）で、IATA コードで指定した路線、出発日、目標価格を設定し、路線を有効にして保存します。
2. 通知が必要なら、自分の DingTalk Webhook と、必要に応じて署名用シークレットを登録します。テスト送信（`测试推送`）で届くことを確認してください。[DingTalk の設定手順（英語）](README_EN.md#dingtalk-robot-setup)も参照できます。
3. ダッシュボードで取得価格と履歴を確認します。一部のチャネルはログインが必要です。購入前には販売元で実際の価格と空席を確認してください。

Web チャネルは Windows、macOS、Linux で利用できます。任意の Android アプリ用チャネルを使うには Windows と MuMu が必要です。管理画面には認証機能がないため、既定のローカル待ち受けを維持し、インターネットに公開しないでください。通知は設定した外部サービスへ送信されます。レポート画像の外部ホスティングを使う前に [セキュリティ方針（中国語）](SECURITY.md) を確認し、各サービスの利用規約に従ってください。

[Apache License 2.0](LICENSE) · [貢献者](CONTRIBUTORS.md)
