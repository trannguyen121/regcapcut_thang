import subprocess
import unittest

from modules.proxy.expressvpn import (
    ExpressVPNController,
    ExpressVPNStatus,
)


class ExpressVPNControllerTests(unittest.TestCase):
    def test_smart_location_omits_invalid_flag(self):
        controller = ExpressVPNController(binary_path="fake-expressvpn")
        commands = []

        def fake_run(args, timeout=None):
            commands.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        controller._run = fake_run
        controller.get_status = lambda: ExpressVPNStatus(connected=True, alias="Smart")

        status = controller.connect(wait_seconds=0)

        self.assertTrue(status.connected)
        self.assertEqual(commands, [["connect"]])

    def test_locations_keep_full_connect_name_and_id(self):
        output = """VPN Location Name       VPN Location Id
Asia Pacific:
  Singapore - Jurong      154
  Australia - Sydney      166
"""

        locations = ExpressVPNController._parse_locations(output)

        self.assertEqual(locations[0].alias, "Singapore - Jurong")
        self.assertEqual(locations[0].location_id, "154")
        self.assertEqual(locations[0].country, "Singapore")

    def test_connected_status_keeps_full_location(self):
        status = ExpressVPNController._parse_status("Connected to Singapore - Jurong")

        self.assertTrue(status.connected)
        self.assertEqual(status.alias, "Singapore - Jurong")
        self.assertEqual(status.country, "Singapore")


if __name__ == "__main__":
    unittest.main()
