# 箱根駅伝配車シミュレーター

箱根駅伝の応援に参加するサークルメンバーへの配車計画を自動生成するツールです。  
Googleフォームで収集した参加者情報をもとに、MILPによる最適化で1〜10区＋帰路の配車を計算します。

## 機能

- Googleフォームの回答スプレッドシートを直接読み込み
- PuLP (MILP) による配車最適化（レンタカー台数・乗り換え回数・ランナー希望区間を考慮）
- 結果を元のスプレッドシートの「配車結果」シートに書き出し
- Webブラウザ UI で操作（コマンド不要）

## セットアップ

### 1. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

### 2. Google Cloud の設定

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成
2. **Google Sheets API** と **Google Drive API** を有効化
3. サービスアカウントを作成し、JSON キーをダウンロード
4. ダウンロードした JSON を `credentials.json` という名前でこのフォルダに置く

### 3. Googleフォームの質問構成

[フォームリンク](https://docs.google.com/forms/d/e/1FAIpQLSdpxEPKW0NL6yjhgsj4tR0wDNWNr4xl-V82frrve7fb4Z33Kg/viewform?usp=publish-editor)

| 質問文 | 形式 | 選択肢 |
|--------|------|--------|
| 名前 | 短文 | |
| 学年 | ラジオボタン | 1年生 / 2年生 / 3年生 / 4年生 / 5年生以上 |
| 走りたい区間を全て選んでください | チェックボックス | 1区〜10区 |
| 何区間まで走れますか？ | プルダウン | 1〜10 |
| 普通自動車の運転ができますか？ | ラジオボタン | はい / いいえ |
| 8人乗りのバン（大型）の運転ができますか？ | ラジオボタン | はい / いいえ |
| 山道の運転ができますか？ | ラジオボタン | はい / いいえ |
| 1日目の夜に宿泊しますか？ | ラジオボタン | はい（宿泊する） / いいえ（日帰り） |
| 日帰りの方：何区の後に帰りますか？ | 短文（任意） | 例: 5（宿泊する方は空欄） |

> **注意:** 選択肢の「はい」で始まる文言をそのまま使ってください。コードは回答が「はい」で始まるかで判定しています。

### 4. スプレッドシートの共有

フォームの回答スプレッドシートを、`credentials.json` 内の `client_email` と**編集者**として共有してください。

```bash
# client_email の確認
python3 -c "import json; print(json.load(open('credentials.json'))['client_email'])"
```

## 使い方

### Web UI（推奨）

```bash
.venv/bin/streamlit run app.py
```

ブラウザで http://localhost:8501 を開き、「Googleフォームの回答」を選択してスプレッドシートの URL を貼り付け、「シミュレーション実行」を押すだけです。

### コマンドライン

```bash
# Googleフォームの回答スプレッドシートを使う場合
.venv/bin/python main.py --url "https://docs.google.com/spreadsheets/d/..."

# ローカルの CSV を使う場合
.venv/bin/python main.py
```

## ファイル構成

```
hakone/
├── app.py                  # Streamlit による Web UI
├── main.py                 # CLI エントリーポイント
├── models.py               # データクラス定義
├── validator.py            # 配車結果の検証
├── requirements.txt
├── credentials.json        # サービスアカウントキー（.gitignore 済み）
├── data_io/
│   ├── csv_manager.py      # CSV の読み込み
│   └── sheets_manager.py   # Google Sheets の読み書き
└── logic/
    ├── car_pool.py         # 車両プール定数（大型4台・普通4台）
    ├── allocator.py        # 貪欲ヒューリスティック（フォールバック）
    └── milp_allocator.py   # MILP 最適化（メイン）
```

## 最適化の目的関数

| 重み | 内容 |
|-----:|------|
| 5000 | レンタカー台数（少ないほど良い） |
| 15000 | 借りた車を使わない区間（駐車ペナルティ） |
| 500 | 山行きランナーが 7・8 区で運転する |
| 200 | 前区間のランナーが次区間を運転する |
| 100 | 2 年生以上が一人も乗らない車 |
| 50 | ランナーの希望区間が叶わない |
| 50 | 定員に対して乗車人数が少ない |
| 30 | 先行車への人数の偏り |
| 5 | 区間をまたいで乗り換える |
