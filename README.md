# duofu-picks

一个自动同步开源项目 Release 的 AltStore 源。

源地址：https://duofuwang.github.io/duofu-picks/repo.json

只改 `config.json` 就行。

```bash
export GITHUB_TOKEN=$(gh auth token)
python build.py
python build.py --check
python build.py --allow-shrink --prune
```
