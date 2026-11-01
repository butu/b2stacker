from astrostack import system


def test_tasklist_german_none_running():
    # deutsches Windows, OEM-Codepage cp850: "ü" = 0x81
    raw = 'INFORMATION: Es werden keine Aufgaben mit den angegebenen Kriterien ausgeführt.'.encode("cp850")
    assert b"\x81" in raw
    assert system.parse_tasklist(system._decode(raw)) == []


def test_tasklist_running_german_number_format():
    raw = '"PixInsight.exe","12345","Console","1","9.876.543 K"\r\n'.encode("cp850")
    r = system.parse_tasklist(system._decode(raw))
    assert r[0]["pid"] == 12345 and round(r[0]["mem_mb"]) == 9645
