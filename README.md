# arknights-tts

アークナイツ（Yostar 日本版）のメインストーリーを VOICEVOX で読み上げて、章ごとの **M4B（オーディオブック）+ SRT** にするツールです。

本文データは [Kengxxiao/ArknightsGameData_YoStar](https://github.com/Kengxxiao/ArknightsGameData_YoStar)（2025-12-25 にアーカイブされ read-only）から取得します。アーカイブ時点で **序章〜第 15 章まで対応**。それ以降に追加された章には未対応です。

> **個人利用専用**: アークナイツ本文は Hypergryph / Yostar、東北きりたんは SSS LLC の権利物です。生成した音声・字幕を**配布・公開しないでください**。本リポジトリのコード自体は MIT。

> ⚠️ **vibe-coded**: このプロジェクトは Claude を使って書かれており、十分な検証を経ていないバグを含む可能性があります

## Requirements

| | |
|---|---|
| OS | Windows 10/11 |
| Python | 3.11 以上 |
| Git | 2.40 以上 |
| GPU | 任意（CPU 版 VOICEVOX でも動作） |

## Setup

### 1. 依存ツール

```powershell
python -m pip install --user uv          # Python パッケージマネージャ
winget install Gyan.FFmpeg               # M4B 結合
winget install HiroshibaKazuyuki.VOICEVOX  # CPU しかない場合は .CPU を末尾に
```

PowerShell を開き直して `ffmpeg -version` と `uv --version` が通ることを確認してください。

### 2. プロジェクト

```powershell
git clone https://github.com/<your-fork>/arknights-tts.git
cd arknights-tts
uv sync
```

### 3. ストーリーデータ取得（1 回限り）

```powershell
mkdir data\upstream
cd data\upstream
git clone --filter=blob:none --no-checkout https://github.com/Kengxxiao/ArknightsGameData_YoStar.git .
git sparse-checkout init --cone
git sparse-checkout set ja_JP/gamedata/story ja_JP/gamedata/excel
git checkout main
cd ..\..
uv run arknights-tts index
```

### 4. VOICEVOX エンジン起動

`%LOCALAPPDATA%\Microsoft\WinGet\Packages\HiroshibaKazuyuki.VOICEVOX_*\VOICEVOX\vv-engine\run.exe` を実行（GUI 不要）。`http://127.0.0.1:50021/version` が応答すれば OK。

### 5. 単語辞書を VOICEVOX に登録

```powershell
uv run arknights-tts dict sync
```

固有名詞 70 件が VOICEVOX のユーザ辞書に追加されます（`[arknights_tts]` マーカー付き、既存の自分の辞書とは無干渉）。

## Usage

章ごとに `parse → synth → build` を一括実行:

```powershell
uv run arknights-tts all main_00       # 序章
uv run arknights-tts all main_01..main_03  # 1-3 章
uv run arknights-tts all --all         # 全章（GPU で 30〜100 時間）
```

完成すると `data/output/main_NN.m4b` と `main_NN.srt` ができます。スマホに転送して BookPlayer (iOS) や Smart AudioBook Player (Android) などで再生してください。

その他のコマンドは `uv run arknights-tts --help` で確認できます（`pick` で対話選択、`list` で章一覧、`cache stats` でキャッシュ統計、など）。

## 文と文の間隔を変える（オプション）

`config/preset_config.json` の `pacing_ms` を編集して `arknights-tts build <chapter>` を再実行するだけです（合成キャッシュは無効化されません）:

```json
"pacing_ms": {
  "same_speaker": 500,        // 同一話者の連続発話 (ms)
  "speaker_switch": 600,      // 話者交代
  "narration_dialogue": 750,  // 地の文 ↔ セリフ
  "scene": 1500,              // 場面転換 (Blocker)
  "beg_end": 2500,            // 戦闘前 → 戦闘後
  "stage_gap": 4500           // ステージ間
}
```

## 誤読を直す

聴いていて固有名詞の読み間違いに気付いたら:

1. `config/word_dict.csv` に正しい読みを追記
2. `uv run arknights-tts dict sync`
3. `uv run arknights-tts cache invalidate-by-dict --old <旧 CSV のパス>`（影響行のキャッシュ削除）
4. `uv run arknights-tts synth main_NN && uv run arknights-tts build main_NN`

## License

- コード: [MIT](LICENSE)
- 生成された音声・字幕: 配布禁止（VOICEVOX キャラクター利用規約と原作著作権による）
