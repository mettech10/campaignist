from dotenv import load_dotenv

load_dotenv()  # no-op in production; Render injects real env vars

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
