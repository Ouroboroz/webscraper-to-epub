import os

from epub_scraper.tools import fetch_sample
from fakes import FakeResponse, FakeSession

URL = "https://www.fanmtl.com/novel/abc.html"


def run(monkeypatch, argv, session):
    monkeypatch.setattr("sys.argv", ["fetch_sample"] + argv)
    monkeypatch.setattr(fetch_sample.requests, "Session", lambda: session)
    fetch_sample.main()


def test_fetch_sample_writes_out_file(monkeypatch, tmp_path, capsys):
    out = str(tmp_path / "page.html")
    session = FakeSession({URL: FakeResponse("<html>hi</html>", 200, URL)})

    run(monkeypatch, [URL, "--out", out], session)

    assert os.path.exists(out)
    with open(out) as f:
        assert f.read() == "<html>hi</html>"
    assert "Saved" in capsys.readouterr().err


def test_fetch_sample_prints_to_stdout_when_no_out_given(monkeypatch, capsys):
    session = FakeSession({URL: FakeResponse("<html>hi</html>", 200, URL)})
    run(monkeypatch, [URL], session)
    assert "<html>hi</html>" in capsys.readouterr().out
