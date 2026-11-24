"""conftest restores os.environ after every test, so a direct write cannot reach the next test."""
import os

LEAK = "SOTTO_TEST_ENV_ISOLATION_PROBE"


def test_a_direct_environment_write_stays_inside_its_test():
    os.environ[LEAK] = "1"
    os.environ["SOTTO_DATA"] = "/nonexistent/leaked-by-a-test"
    os.environ.pop("HOME", None)


def test_the_next_test_sees_the_environment_as_it_was():
    assert LEAK not in os.environ
    assert os.environ.get("SOTTO_DATA") != "/nonexistent/leaked-by-a-test"
    assert "HOME" in os.environ
