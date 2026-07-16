"""Run the engine with `python -m engine_api`."""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("engine_api.app:app", host="127.0.0.1", port=8100, reload=False)
