"""Read-only financial routing: production evidence is tested by the AD owner."""
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.product import service
from tests.test_product_api import configuration as product_configuration

configuration = product_configuration


def test_financial_routes_keep_cutoff_mode_and_do_not_create_jobs(configuration, monkeypatch):
    calls=[]
    monkeypatch.setattr(service.Product,'financial_point',lambda self,request: calls.append(request.model_dump(mode='json')) or {'status':'ok'})
    monkeypatch.setattr(service.Product,'financial_coverage',lambda self,security,day: {'security':security,'day':day.isoformat() if day else None})
    app=create_app(str(configuration.control_db),product_configuration=configuration)
    with TestClient(app) as client:
        body=dict(security='600126.XSHG',field='operating_revenue',period='2026-03-31',source='cninfo',as_of='2026-04-27T00:00:00+08:00')
        assert client.post('/api/v1/product/financial/point',json=body).status_code==200
        assert calls[0]['mode']=='official_daily'
        assert client.post('/api/v1/product/financial/point',json=dict(body,as_of='2026-04-27')).status_code==422
        assert client.post('/api/v1/product/financial/point',json=dict(body,statement='lrb')).status_code==422
        assert client.post('/api/v1/product/financial/point',json=body,headers={'Origin':'https://example.com'}).status_code==403
        response=client.get('/api/v1/product/financial/coverage',params={'security':'001914.XSHE','day':'2012-01-04'})
        assert response.status_code==200 and response.json()['day']=='2012-01-04'
        assert client.get('/api/v1/product/financial/coverage',params={'unexpected':1}).status_code==422
