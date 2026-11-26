"""Run the same public-page check exposed in Swagger UI."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.public_pages import check_pages

if __name__ == '__main__':
    report = check_pages(['70030076177928867', '70030076180207546', '70030076191027893'])
    print(json.dumps(report, ensure_ascii=True, indent=2))
