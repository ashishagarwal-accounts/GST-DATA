from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import migrate
from app.auth import require_user
from app.routers import advances, auth, cost_centres, dashboard, entities, gstr1, imports

WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.upgrade()  # apply any pending schema migrations to DATABASE_URL
    yield


app = FastAPI(title="Alcove GST Tool", version="0.1.0", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json")
app.include_router(auth.router, prefix="/api")  # sign-in endpoints; user admin routes check rights themselves
for module in (dashboard, entities, imports, advances, gstr1, cost_centres):
    app.include_router(module.router, prefix="/api", dependencies=[Depends(require_user)])
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.middleware("http")
async def revalidate_web_files(request: Request, call_next):
    """Browsers must re-check the page and its static files on every load (cheap: 304 when unchanged),
    so an update to app.js / app.css can never be hidden behind a stale cached copy."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def index():
    # Version-tag the script and stylesheet URLs with their modification time as a second safeguard.
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.css", "app.js"):
        version = int((WEB_DIR / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
    return HTMLResponse(html)
