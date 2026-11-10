import pytest

from app import rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """限流计数是进程级的，每个测试前后清空，互不影响。"""
    rate_limit.reset_all()
    yield
    rate_limit.reset_all()
