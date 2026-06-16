from scout.llm import FakeLLMClient


def test_fake_returns_queued_and_records_calls():
    fake = FakeLLMClient(["first", "second"])
    assert fake.complete("p1") == "first"
    assert fake.complete("p2", web=True) == "second"
    assert fake.calls[0].prompt == "p1"
    assert fake.calls[1].web is True
