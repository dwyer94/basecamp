from __future__ import annotations

from basecamp.console import Console, FileTailer, LineSource, RotatingFile
from tests.conftest import wait_for


def test_ring_buffer_keeps_last_lines(tmp_path):
    console = Console(tmp_path / "console.log", max_lines=3)
    for i in range(5):
        console.write(f"line {i}")

    lines = console.lines()

    assert [ln.text for ln in lines] == ["line 2", "line 3", "line 4"]
    assert [ln.text for ln in console.lines(after_seq=lines[1].seq)] == ["line 4"]
    console.close()
    assert "line 0" in (tmp_path / "console.log").read_text(encoding="utf-8")


def test_sources_are_marked_in_the_log(tmp_path):
    console = Console(tmp_path / "console.log")
    console.write("server says hi")
    console.write("save", LineSource.IN)
    console.system("Started")
    console.close()

    text = (tmp_path / "console.log").read_text(encoding="utf-8")

    assert " server says hi" in text and "> save" in text and "[basecamp] Started" in text


def test_subscribers_get_lines_until_unsubscribed(tmp_path):
    console = Console(tmp_path / "console.log")
    got = []
    unsubscribe = console.subscribe(lambda line: got.append(line.text))
    console.write("one")
    unsubscribe()
    console.write("two")
    console.close()

    assert got == ["one"]


def test_bad_subscriber_doesnt_break_console(tmp_path):
    console = Console(tmp_path / "console.log")
    console.subscribe(lambda line: 1 / 0)
    console.write("still fine")
    console.close()

    assert console.lines()[-1].text == "still fine"


def test_log_file_rotates(tmp_path):
    path = tmp_path / "console.log"
    log = RotatingFile(path, max_bytes=100, backups=2)
    for i in range(30):
        log.write(f"line number {i:03d}\n")
    log.close()

    assert path.exists()
    assert (tmp_path / "console.log.1").exists() and (tmp_path / "console.log.2").exists()
    assert not (tmp_path / "console.log.3").exists()
    assert "line number 029" in path.read_text() + (tmp_path / "console.log.1").read_text()


def test_lines_from_a_host_are_not_written_again(tmp_path):
    console = Console(tmp_path / "console.log")
    console.write("from the host", ts=123.0, to_file=False)
    console.system("from basecamp")
    console.close()

    assert console.lines()[0].ts == 123.0
    text = (tmp_path / "console.log").read_text(encoding="utf-8")
    assert "from the host" not in text and "from basecamp" in text


def test_tailer_follows_appends_truncation_and_late_file(tmp_path):
    path = tmp_path / "game.log"
    path.write_text("old\n", encoding="utf-8")
    console = Console(tmp_path / "console.log")
    tailer = FileTailer(path, console.write, interval=0.05).start()
    texts = lambda: [ln.text for ln in console.lines()]  # noqa: E731
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("new 1\nnew ")
            f.flush()
            wait_for(lambda: "new 1" in texts())
            f.write("2\n")
        wait_for(lambda: "new 2" in texts())

        path.write_text("after truncate\n", encoding="utf-8")
        wait_for(lambda: "after truncate" in texts())

        path.unlink()
        path.write_text("recreated\n", encoding="utf-8")
        wait_for(lambda: "recreated" in texts())
    finally:
        tailer.stop()
        console.close()

    assert "old" not in texts()


def test_tailer_waits_for_missing_file(tmp_path):
    path = tmp_path / "later.log"
    console = Console(tmp_path / "console.log")
    tailer = FileTailer(path, console.write, interval=0.05).start()
    try:
        path.write_text("hello\n", encoding="utf-8")
        wait_for(lambda: [ln.text for ln in console.lines()] == ["hello"])
    finally:
        tailer.stop()
        console.close()
