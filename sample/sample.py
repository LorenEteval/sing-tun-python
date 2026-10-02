"""Installed-wheel import/provenance/API smoke; never opens a TUN."""

import argparse
import pathlib

import sing_tun


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outside", type=pathlib.Path)
    args = parser.parse_args()
    if args.outside:
        assert (
            pathlib.Path(sing_tun.__file__).resolve().parent
            != (args.outside / "sing_tun").resolve()
        )
    assert sing_tun.__upstream_version__.startswith("v")
    assert "v" + sing_tun.__version__ == sing_tun.__upstream_version__
    assert len(sing_tun.__upstream_commit__) == 40
    if args.outside:
        for attribute, filename in (
            ("__version__", "VERSION"),
            ("__upstream_version__", "UPSTREAM_VERSION"),
            ("__upstream_commit__", "UPSTREAM_COMMIT"),
        ):
            assert (
                getattr(sing_tun, attribute)
                == (args.outside / filename).read_text().strip()
            )
    config = sing_tun.Config(proxy="socks5://127.0.0.1:1080")
    engine = sing_tun.Engine(config)
    assert engine.status["state"] == "created"
    assert not engine.ready
    engine.close()
    print(
        sing_tun.__version__,
        sing_tun.__upstream_version__,
        sing_tun.__upstream_commit__,
    )
    print(sing_tun.capabilities())


if __name__ == "__main__":
    main()
