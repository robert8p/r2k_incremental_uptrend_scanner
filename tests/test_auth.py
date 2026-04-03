from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import TokenAuthMiddleware


def test_sensitive_route_without_token_returns_clean_401_response():
    app = FastAPI()
    app.add_middleware(TokenAuthMiddleware, auth_token='secret-token')

    @app.get('/api/research/41')
    def research_detail():
        return {'ok': True}

    client = TestClient(app)
    response = client.get('/api/research/41')

    assert response.status_code == 401
    assert response.json() == {'detail': 'Unauthorized. Provide a valid token.'}
