# Issues

Active bugs (B-xx), improvements (F-xx), and test findings (T-xx).
Resolved issues are moved to `docs/issues-resolved.md`.

---

## Bugs

<!-- B-01: ... -->

*(なし)*

---

## Improvements

### F-01: handle_message のロック内で Slack API を呼んでいる可能性がある（優先度：最低、要再検討）

- **対象**: `src/bot.py` `handle_message` ステップ6
- **内容**: DDの記述上「`with state.lock:` 内で `is_processing = True` なら返信して return」となっており、Coderがロック内で `chat_postMessage` を呼ぶ実装をする可能性がある。ロック保持中にネットワークI/Oが発生すると、`_watch_for_response` の `finally` 節がロック取得を待たされる。
- **実害**: Slack API が正常なら数百ms の遅延のみ。機能的誤動作は発生しない。タイムアウト・障害時に `is_processing` クリアが数秒〜数十秒遅れる可能性がある。
- **対処方針**: `with state.lock:` 内ではメモリ操作のみ行い、`chat_postMessage` はロックを抜けてから呼ぶよう DDまたはコメントで明示する。
- **優先度**: 最低（実運用上の影響が限定的なため、Coding後または別 feature で対応を検討）
- **発見**: feature000 設計レビュー中（2026-07-25）

---

## Notes

- B-xx = バグ（動作が仕様と異なる）
- F-xx = 改善要望（仕様上は問題ないが運用上の課題）
- T-xx = テスト起因の発見事項
