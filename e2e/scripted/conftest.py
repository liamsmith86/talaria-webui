"""Run these tests against the scripted model, by overriding the shared fixtures' stack."""

import pytest


@pytest.fixture(scope="session")
def hermes(scripted_hermes):
    return scripted_hermes


@pytest.fixture(scope="session")
def talaria(scripted_talaria):
    return scripted_talaria
