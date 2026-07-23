# Ice.agent backend

Run locally:

```powershell
python -m pip install -e ".[test,mem0]"
uvicorn app.main:app --reload
```

If global pytest plugins interfere, run the isolated suite with:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -p pytest_asyncio.plugin
```
