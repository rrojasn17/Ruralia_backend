"""Ejercita la política CORS real, sin el comodín de los fixtures de interfaz."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from main import app


def test_shared_field_csrf_preflight_is_allowed():
    policy = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    isolated = FastAPI()
    isolated.add_middleware(policy.cls, **policy.kwargs)
    origin = policy.kwargs['allow_origins'][0]
    with TestClient(isolated) as client:
        for operation in ('planting', 'harvest', 'logout'):
            response = client.options(f'/floresvolcan/public/field/{operation}', headers={
                'Origin': origin,
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'content-type,x-fv-csrf',
            })
            assert response.status_code == 200
            assert response.headers['access-control-allow-origin'] == origin
            assert response.headers['access-control-allow-credentials'] == 'true'
            assert 'x-fv-csrf' in response.headers['access-control-allow-headers'].lower()
