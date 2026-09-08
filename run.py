from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402  (must follow load_dotenv)

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
