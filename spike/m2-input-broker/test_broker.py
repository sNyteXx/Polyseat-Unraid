#!/usr/bin/env python3
"""What the broker decides, checked without a seat to decide it about.

Attribution is the one part of this that can be wrong quietly. A device that
does not arrive is a complaint within the minute; a device that arrives in the
wrong seat is somebody else's mouse on your screen, and the first person to
notice may be the one it was taken from. So the rules live behind two functions
that can be asked directly, and this asks them with the names real builds
produce rather than with invented ones.

    python3 -m unittest discover -s spike/m2-input-broker
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import broker


class TheNamesRealBuildsProduce(unittest.TestCase):
    """contradicts() against every device name seen on the machine this was
    written on, from both Sunshine builds and both controllers."""

    SEATS = ("joser", "vince")

    def test_a_name_with_no_parentheses_makes_no_claim(self):
        # The libvirtualhid build. There is nothing here to agree or disagree
        # with, and treating that as disagreement is exactly the bug.
        for name in ("libvirtualhid Mouse", "libvirtualhid Keyboard",
                     "libvirtualhid Touchscreen", "libvirtualhid Pen Tablet",
                     "Wireless Controller", "Wireless Controller Touchpad"):
            self.assertFalse(broker.contradicts(name, "vince", self.SEATS), name)
            self.assertFalse(broker.contradicts(name, "joser", self.SEATS), name)

    def test_our_own_tag_does_not_contradict_us(self):
        self.assertFalse(
            broker.contradicts("Mouse passthrough (vince)", "vince", self.SEATS))

    def test_a_word_in_brackets_that_is_not_a_seat_means_nothing(self):
        # The case that broke the first version of this, found on a real device
        # rather than by thinking: the pad Sunshine emulates for a seat is
        # called "Sunshine (libvirtualhid) X-Box Series Controller". A rule that
        # counts any bracketed word as a foreign tag refuses that seat its own
        # controller. "(virtual)" and "(absolute)" are the same kind of word.
        for name in ("Sunshine (libvirtualhid) X-Box Series Controller",
                     "Mouse passthrough (vince) (absolute)",
                     "Sunshine X-Box One (virtual) pad"):
            self.assertFalse(broker.contradicts(name, "vince", self.SEATS), name)

    def test_a_tag_that_is_not_last_still_counts(self):
        self.assertFalse(
            broker.contradicts("Sunshine X-Box One (virtual) pad (seat1)",
                               "seat1", ("seat1", "seat2")))

    def test_another_seats_device_does_contradict(self):
        self.assertTrue(
            broker.contradicts("Mouse passthrough (joser) (absolute)", "vince",
                               self.SEATS))
        self.assertTrue(
            broker.contradicts("Sunshine X-Box One (virtual) pad (seat1)",
                               "seat2", ("seat1", "seat2")))

    def test_with_no_other_seats_named_nothing_can_contradict(self):
        # One seat on the host, or a daemon that did not pass the list. The
        # structural answer then decides alone, which is the safe direction.
        self.assertFalse(broker.contradicts("Mouse passthrough (joser)", "vince", ()))
        self.assertFalse(broker.contradicts("anything at all", None, ()))


class WhoOwnsADevice(unittest.TestCase):
    """attribute() with the helper it asks answering from a fixture.

    The uinput case is the regression this file exists for: keyboards and mice
    are traced to their creator's cgroup and the name is not consulted, so a
    Sunshine that stopped writing seat tags must not change the answer.
    """

    def setUp(self):
        self._owner = broker.device_owner
        broker._reported.clear()
        broker._uhid_seen = {}
        broker._correlated.clear()

    def tearDown(self):
        broker.device_owner = self._owner

    def _fixture(self, owners, holders=None):
        class Owner:
            @staticmethod
            def owners():
                return owners

            @staticmethod
            def uhid_holders():
                return holders or {}

        broker.device_owner = Owner

    def test_an_untagged_uinput_device_goes_to_the_seat_that_made_it(self):
        self._fixture({"input172": "joser"})

        mine = broker.attribute(
            {"event28": {"sysname": "input172", "name": "libvirtualhid Mouse",
                         "syspath": "/sys/devices/virtual/input/input172"}},
            "joser", "joser")

        self.assertIn("event28", mine)
        self.assertEqual(mine["event28"]["attribution"], "owner")

    def test_the_same_device_does_not_go_to_the_other_seat(self):
        self._fixture({"input172": "joser"})

        mine = broker.attribute(
            {"event28": {"sysname": "input172", "name": "libvirtualhid Mouse",
                         "syspath": "/sys/devices/virtual/input/input172"}},
            "vince", "vince")

        self.assertEqual(mine, {})

    def test_a_device_made_on_the_host_belongs_to_nobody(self):
        # None is the host, and it stays refused however it is named.
        self._fixture({"input9": None})

        mine = broker.attribute(
            {"event9": {"sysname": "input9", "name": "Mouse passthrough (vince)",
                        "syspath": "/sys/devices/virtual/input/input9"}},
            "vince", "vince")

        self.assertEqual(mine, {})


class AGamepadStaysWithoutTheObserver(unittest.TestCase):
    """Without the uhid observer (no bpftrace on Unraid) a gamepad is correlated to
    the one container that opened a uhid descriptor in that cycle. The next cycle
    has no new descriptor; the gamepad must stay where it was put rather than be
    taken away a second after attaching (Tower run 29)."""

    PAD = {"event19": {"sysname": "input40", "name": "Sunshine (libvirtualhid) X-Box Series Controller",
                       "syspath": "/sys/devices/virtual/misc/uhid/0003:045E:0B12.0001/input/input40"}}

    def setUp(self):
        self._owner, self._observer = broker.device_owner, broker.uhid_observer
        broker._reported.clear()
        broker._uhid_seen = {}
        broker._correlated.clear()
        self.holders = {}
        holders = self.holders

        class Owner:
            @staticmethod
            def owners():
                return {}

            @staticmethod
            def uhid_holders():
                return dict(holders)

        class Observer:
            @staticmethod
            def owner_of_node(node):
                return "unknown"

        broker.device_owner, broker.uhid_observer = Owner, Observer

    def tearDown(self):
        broker.device_owner, broker.uhid_observer = self._owner, self._observer

    def test_the_pad_stays_with_the_seat_it_was_correlated_to(self):
        self.holders["fd1"] = "seat1"
        self.assertIn("event19", broker.attribute(dict(self.PAD), "seat1", "seat1"))
        for _ in range(3):  # later cycles: nothing new opened
            self.assertIn("event19", broker.attribute(dict(self.PAD), "seat1", "seat1"))

    def test_another_seat_never_gets_it_and_a_new_device_is_judged_afresh(self):
        self.holders["fd1"] = "seat2"
        self.assertEqual(broker.attribute(dict(self.PAD), "seat1", "seat1"), {})
        self.assertEqual(broker.attribute(dict(self.PAD), "seat1", "seat1"), {})
        broker.attribute({}, "seat1", "seat1")  # the pad is gone: its answer goes with it
        self.assertEqual(broker._correlated, {})


class SealingReachesWhatTheHostAlreadyHolds(unittest.TestCase):
    """seal() against a sysfs, /dev and udev database built in a directory.

    A seat's DualSense as the kernel lays it out: one uhid device with an event
    node, a joystick node beside it, and a raw HID node. The broker is asked to
    seal it, and what it would have done to the host is recorded rather than
    done.
    """

    PAD = "devices/virtual/misc/uhid/0003:054C:0CE6.0020"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = self.dir.name
        sys_root = os.path.join(root, "sys")
        self.sys_root = sys_root

        inp = f"{sys_root}/{self.PAD}/input/input300"
        for node, number in (("event30", "13:94"), ("js2", "13:2")):
            os.makedirs(f"{inp}/{node}")
            with open(f"{inp}/{node}/dev", "w") as fh:
                fh.write(number + "\n")
        os.makedirs(f"{sys_root}/{self.PAD}/hidraw/hidraw7")
        with open(f"{sys_root}/{self.PAD}/hidraw/hidraw7/dev", "w") as fh:
            fh.write("243:7\n")

        os.makedirs(f"{sys_root}/class/input")
        os.makedirs(f"{sys_root}/class/hidraw")
        os.symlink(f"{inp}/event30", f"{sys_root}/class/input/event30")
        os.symlink(f"{sys_root}/{self.PAD}/hidraw/hidraw7",
                   f"{sys_root}/class/hidraw/hidraw7")

        self.db = os.path.join(root, "udev-data")
        os.makedirs(self.db)

        self._saved = (broker.SYS_ROOT, broker.SYS_INPUT, broker.DEV_ROOT,
                       broker.SEALED, broker.UDEV_DB, broker.udev_trigger,
                       broker.seal_path)
        broker.SYS_ROOT = sys_root
        broker.SYS_INPUT = f"{sys_root}/class/input"
        broker.DEV_ROOT = os.path.join(root, "dev")
        broker.SEALED = os.path.join(root, "sealed")
        broker.UDEV_DB = self.db
        broker._reannounced.clear()

        self.events = []
        self.sealed = []

        def trigger(action, devpath):
            # Whether the mark was there when the event went out is the whole
            # point of the order: the add has to come back hidden.
            marked = os.path.isdir(f"{broker.SEALED}{devpath}")
            self.events.append((action, devpath, marked))

        broker.udev_trigger = trigger
        broker.seal_path = lambda path: self.sealed.append(path) or True

    def tearDown(self):
        (broker.SYS_ROOT, broker.SYS_INPUT, broker.DEV_ROOT, broker.SEALED,
         broker.UDEV_DB, broker.udev_trigger, broker.seal_path) = self._saved
        self.dir.cleanup()

    def hidden(self, number):
        with open(f"{self.db}/c{number}", "w") as fh:
            fh.write("E:ID_INPUT=1\nE:POLYSEAT_HIDDEN=1\n")

    def test_the_joystick_node_is_sealed_too(self):
        # js is how Steam finds a controller, and seal() used to do the event
        # node and the raw HID node and leave this one open.
        broker.seal("event30")

        names = [os.path.basename(p) for p in self.sealed]
        self.assertEqual(names, ["event30", "js2", "hidraw7"])

    def test_a_node_the_rule_missed_is_announced_again_after_it_is_marked(self):
        broker.seal("event30")

        event = f"/{self.PAD}/input/input300/event30"
        self.assertIn(("remove", event, True), self.events)
        self.assertIn(("add", event, True), self.events)
        self.assertLess(self.events.index(("remove", event, True)),
                        self.events.index(("add", event, True)))
        self.assertTrue(all(marked for _, _, marked in self.events))

    def test_a_node_the_rule_hid_is_left_where_it_is(self):
        # Hidden at creation means nothing on the host ever opened it.
        for number in ("13:94", "13:2", "243:7"):
            self.hidden(number)

        broker.seal("event30")

        self.assertEqual(self.events, [])

    def test_a_device_is_announced_again_once(self):
        # A rule file older than the broker never says POLYSEAT_HIDDEN, and the
        # host must not see the pad leave and return twice a second.
        broker.seal("event30")
        count = len(self.events)
        broker.seal("event30")

        self.assertEqual(count, 6)
        self.assertEqual(len(self.events), count)

    def test_marks_of_devices_that_are_gone_are_dropped(self):
        broker.seal("event30")
        os.makedirs(f"{broker.SEALED}/devices/virtual/input/input9/event9")

        broker.unmark_gone()

        self.assertFalse(os.path.exists(
            f"{broker.SEALED}/devices/virtual/input/input9"))
        self.assertTrue(os.path.isdir(
            f"{broker.SEALED}/{self.PAD}/input/input300/js2"))


class APhysicalDevicesWorld(unittest.TestCase):
    """A sysfs built in a directory with a Razer receiver in it, and the
    assignment file beside it. Shared by everything that tests the assignment
    of physical devices."""

    RAZER = "devices/pci0000:00/0000:00:14.0/usb1/1-2"

    # (event node, interface number, name, kind). One receiver, four evdev nodes
    # on three interfaces, as the Naga Pro presents itself.
    NODES = (("event7", 0, "Razer Razer Naga Pro Mouse", "mouse"),
             ("event8", 1, "Razer Razer Naga Pro Keyboard", "keyboard"),
             ("event9", 1, "Razer Razer Naga Pro", "keyboard"),
             ("event10", 2, "Razer Razer Naga Pro Consumer Control", "keyboard"))

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text + "\n")

    def usb_device(self, rel, vendor, product, nodes, first_input=20):
        """A USB device at `rel` with one input device per node entry."""
        top = f"{self.sys_root}/{rel}"
        self.put(f"{top}/idVendor", vendor)
        self.put(f"{top}/idProduct", product)
        for i, (node, iface, name, kind) in enumerate(nodes):
            inp = f"{top}/{os.path.basename(rel)}:1.{iface}/0003:{vendor}:{product}.00{i}/input/input{first_input + i}"
            self.put(f"{top}/{os.path.basename(rel)}:1.{iface}/bInterfaceNumber", f"{iface:02x}")
            self.put(f"{inp}/{node}/dev", f"13:{int(node[5:]) + 64}")
            self.put(f"{inp}/name", name)
            self.put(f"{inp}/capabilities/ev", "17" if kind == "mouse" else "3")
            self.put(f"{inp}/capabilities/key",
                     "10000 0 0 0 0" if kind == "mouse" else "0 0 0 0 10000")
            os.symlink(f"{inp}/{node}", f"{self.sys_root}/class/input/{node}")
            os.symlink(inp, f"{inp}/{node}/device")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sys_root = os.path.join(self.dir.name, "sys")
        os.makedirs(f"{self.sys_root}/class/input")
        self.usb_device(self.RAZER, "1532", "00a4", self.NODES)

        self.assign = os.path.join(self.dir.name, "assign.json")
        self._saved = (broker.SYS_ROOT, broker.SYS_INPUT, broker.ASSIGN_FILE,
                       broker._assign_said)
        broker.SYS_ROOT = self.sys_root
        broker.SYS_INPUT = f"{self.sys_root}/class/input"
        broker.ASSIGN_FILE = self.assign
        broker._assign_said = None

    def tearDown(self):
        (broker.SYS_ROOT, broker.SYS_INPUT, broker.ASSIGN_FILE,
         broker._assign_said) = self._saved
        self.dir.cleanup()

    def write(self, data):
        with open(self.assign, "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def razer(self, seat="seat1", **extra):
        return dict({"vendor": "1532", "product": "00a4", "seat": seat}, **extra)


class TheAssignmentFile(APhysicalDevicesWorld):
    """load_assignments(): strict, and failing towards 'nothing assigned'."""

    def test_a_valid_entry_is_loaded(self):
        self.write([self.razer()])
        self.assertEqual(broker.load_assignments(),
                         {("1532", "00a4"): ("seat1", None)})

    def test_interfaces_restrict_the_assignment(self):
        self.write([self.razer(interfaces=[0])])
        self.assertEqual(broker.load_assignments(),
                         {("1532", "00a4"): ("seat1", frozenset({0}))})

    def test_a_missing_file_assigns_nothing_and_says_so_once(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(broker.load_assignments(), {})
            self.assertEqual(broker.load_assignments(), {})
        self.assertEqual(out.getvalue().count("does not exist"), 1)

    def test_garbage_assigns_nothing_and_does_not_raise(self):
        for text in ("", "{", "null", '{"vendor": "1532"}', "[1]", '["x"]'):
            broker._assign_said = None
            self.write(text)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(broker.load_assignments(), {}, text)

    def test_an_unreadable_file_assigns_nothing(self):
        os.mkdir(self.assign)       # open() on a directory is an OSError
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(broker.load_assignments(), {})

    def test_every_field_is_checked(self):
        bad = [self.razer(vendor="1532 "), self.razer(vendor="1532x"),
               self.razer(vendor="ABCD"), self.razer(vendor=1532),
               self.razer(product="a4"), self.razer(product=None),
               {"vendor": "1532", "seat": "seat1"},          # no product
               {"product": "00a4", "seat": "seat1"},         # no vendor
               {"vendor": "1532", "product": "00a4"},        # no seat
               self.razer(seat="Seat1"), self.razer(seat="a"),
               self.razer(seat="seat1; rm -rf"), self.razer(seat=""),
               self.razer(interfaces=[]), self.razer(interfaces="0"),
               self.razer(interfaces=[True]), self.razer(interfaces=[-1]),
               self.razer(extra="x")]
        for entry in bad:
            with self.subTest(entry=entry), contextlib.redirect_stdout(io.StringIO()):
                self.write([entry])
                self.assertEqual(broker.load_assignments(), {})

    def test_one_bad_entry_spoils_the_whole_file(self):
        self.write([self.razer(), self.razer(seat="NOPE", product="0001")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(broker.load_assignments(), {})

    def test_the_same_device_for_two_seats_assigns_to_neither(self):
        self.write([self.razer("seat1"), self.razer("seat2")])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(broker.load_assignments(), {})
        self.assertIn("listed more than once", out.getvalue())

    def test_a_fixed_file_is_reported_again_when_it_breaks_again(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.write("{")
            broker.load_assignments()
            self.write([self.razer()])
            broker.load_assignments()
            self.write("{")
            broker.load_assignments()
        self.assertEqual(out.getvalue().count("unreadable or not JSON"), 2)


class WhichUsbDeviceAnInputNodeIs(APhysicalDevicesWorld):
    def real(self, node):
        return os.path.realpath(f"{broker.SYS_INPUT}/{node}")

    def test_the_ids_and_the_interface_are_found_by_walking_up(self):
        self.assertEqual(broker.usb_identity(self.real("event7")),
                         ("1532", "00a4", 0))
        self.assertEqual(broker.usb_identity(self.real("event10")),
                         ("1532", "00a4", 2))

    def test_a_node_without_a_usb_ancestor_has_no_identity(self):
        inp = f"{self.sys_root}/devices/virtual/input/input99"
        self.put(f"{inp}/event30/dev", "13:94")
        os.symlink(f"{inp}/event30", f"{self.sys_root}/class/input/event30")
        self.assertIsNone(broker.usb_identity(self.real("event30")))

    def test_only_the_seat_it_is_assigned_to_gets_an_answer(self):
        table = {("1532", "00a4"): ("seat1", None)}
        real = self.real("event7")
        self.assertEqual(broker.assigned_to(real, "seat1", table), "1532:00a4")
        self.assertIsNone(broker.assigned_to(real, "seat2", table))
        self.assertIsNone(broker.assigned_to(real, "seat1", {}))

    def test_the_exact_product_is_required(self):
        real = self.real("event7")
        self.assertIsNone(broker.assigned_to(
            real, "seat1", {("1532", "00a5"): ("seat1", None)}))
        self.assertIsNone(broker.assigned_to(
            real, "seat1", {("1533", "00a4"): ("seat1", None)}))

    def test_interfaces_pick_nodes(self):
        table = {("1532", "00a4"): ("seat1", frozenset({0}))}
        self.assertIsNotNone(broker.assigned_to(self.real("event7"), "seat1", table))
        self.assertIsNone(broker.assigned_to(self.real("event8"), "seat1", table))


class FakeBackend:
    """Records what the broker asked a container runtime to do."""

    def __init__(self):
        self.nodes = {}         # seat -> set of attached nodes
        self.calls = []

    def attach_node(self, seat, node, major, minor, raw=None):
        self.calls.append(("attach", seat, node, raw))
        self.nodes.setdefault(seat, set()).add(node)

    def detach_node(self, seat, node):
        self.calls.append(("detach", seat, node))
        self.nodes.setdefault(seat, set()).discard(node)

    def attached_nodes(self, seat):
        return sorted(self.nodes.get(seat, ()))

    def run(self, seat, argv, stdin=None):
        self.calls.append(("run", seat, argv[0]))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class APhysicalMouseGoesToTheSeatItIsAssignedTo(APhysicalDevicesWorld):
    """scan(), attribute() and the pass that attaches, against the Razer."""

    def setUp(self):
        super().setUp()
        self._owner = broker.device_owner
        broker._reported.clear()
        broker._uhid_seen = {}
        broker._correlated.clear()

        class Owner:
            @staticmethod
            def owners():
                return {}

            @staticmethod
            def uhid_holders():
                return {}

            @staticmethod
            def sysname_of_node(node):
                real = os.path.realpath(f"{broker.SYS_INPUT}/{node}")
                return os.path.basename(os.path.dirname(real))

        broker.device_owner = Owner

        self._env = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.dir.name
        self._sealing = (broker.seal, broker.SEALED)
        self.sealed = []
        broker.seal = lambda node: self.sealed.append(node) or False
        broker.SEALED = os.path.join(self.dir.name, "sealed")
        self.backend = FakeBackend()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        broker.device_owner = self._owner
        broker.seal, broker.SEALED = self._sealing
        if self._env is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._env
        super().tearDown()

    def mine(self, seat, **kw):
        return broker.attribute(broker.scan(seat=seat, **kw), seat, seat)

    def unplug(self, nodes=("event7", "event8", "event9", "event10")):
        for node in nodes:
            os.unlink(f"{self.sys_root}/class/input/{node}")
        shutil.rmtree(f"{self.sys_root}/{self.RAZER}")

    def test_the_assigned_seat_gets_every_node_of_the_device(self):
        self.write([self.razer("seat1")])

        mine = self.mine("seat1")

        self.assertEqual(sorted(mine), ["event10", "event7", "event8", "event9"])
        for dev in mine.values():
            self.assertEqual(dev["attribution"], "assigned")
            self.assertEqual(dev["assigned_seat"], "seat1")
            self.assertEqual(dev["usb_id"], "1532:00a4")
            self.assertIsNone(dev["raw"])

    def test_the_nodes_are_classified_like_any_other(self):
        self.write([self.razer("seat1")])

        mine = self.mine("seat1")

        self.assertIn("ID_INPUT_MOUSE=1", mine["event7"]["props"])
        self.assertNotIn("ID_INPUT_KEYBOARD=1", mine["event7"]["props"])
        self.assertIn("ID_INPUT_KEYBOARD=1", mine["event8"]["props"])
        self.assertEqual(mine["event7"]["name"], "Razer Razer Naga Pro Mouse")
        self.assertEqual((mine["event7"]["major"], mine["event7"]["minor"]), (13, 71))

    def test_the_other_seats_broker_never_gets_it(self):
        self.write([self.razer("seat1")])
        self.assertEqual(self.mine("seat2"), {})
        self.assertEqual(broker.scan(seat="seat2"), {})

    def test_without_a_seat_scan_still_ignores_physical_devices(self):
        self.write([self.razer("seat1")])
        self.assertEqual(broker.scan(), {})

    def test_a_device_nobody_listed_is_still_ignored(self):
        # Same vendor, other product: a different Razer on the same host.
        self.usb_device("devices/pci0000:00/0000:00:14.0/usb1/1-3", "1532", "0099",
                        (("event11", 0, "Razer Other Mouse", "mouse"),), first_input=40)
        self.usb_device("devices/pci0000:00/0000:00:14.0/usb1/1-4", "046d", "c077",
                        (("event12", 0, "Logitech Mouse", "mouse"),), first_input=50)
        self.write([self.razer("seat1")])

        self.assertEqual(sorted(self.mine("seat1")),
                         ["event10", "event7", "event8", "event9"])

    def test_interfaces_narrow_it_to_the_mouse(self):
        self.write([self.razer("seat1", interfaces=[0])])
        self.assertEqual(sorted(self.mine("seat1")), ["event7"])

    def test_a_missing_invalid_or_duplicate_file_attaches_nothing(self):
        for data in (None, "{", [self.razer("seat1"), self.razer("seat2")],
                     [self.razer("seat1"), {"vendor": "1532"}]):
            if data is not None:
                self.write(data)
            for seat in ("seat1", "seat2"):
                self.assertEqual(self.mine(seat), {}, (data, seat))

    def test_a_virtual_device_cannot_borrow_an_assignment(self):
        # The creator check still applies to everything that is not assigned
        # by its USB id: a host-made virtual device is refused as before.
        inp = f"{self.sys_root}/devices/virtual/input/input99"
        self.put(f"{inp}/event30/dev", "13:94")
        self.put(f"{inp}/name", "Razer Razer Naga Pro Mouse")
        os.symlink(f"{inp}/event30", f"{self.sys_root}/class/input/event30")
        os.symlink(inp, f"{inp}/event30/device")
        self.write([self.razer("seat1")])
        broker.device_owner.owners = staticmethod(lambda: {"input99": None})

        self.assertIn("event30", broker.scan(seat="seat1"))    # considered ...
        self.assertNotIn("event30", self.mine("seat1"))        # ... and refused

    def test_a_pass_attaches_without_sealing_and_logs_the_usb_id(self):
        self.write([self.razer("seat1", interfaces=[0])])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            known = broker.cycle(self.backend, "seat1", "seat1", (), None, {})

        self.assertEqual(sorted(known), ["event7"])
        self.assertEqual(self.backend.of("attach"),
                         [("attach", "seat1", "event7", None)])
        self.assertEqual(self.sealed, [])
        self.assertIn("+ event7", out.getvalue())
        self.assertIn("Razer Razer Naga Pro Mouse", out.getvalue())
        self.assertIn("assigned by USB id 1532:00a4: ID_INPUT=1 ID_INPUT_MOUSE=1",
                      out.getvalue())
        # And the synthetic event that makes the seat notice it.
        self.assertIn(("run", "seat1", broker.FAKEUDEV), self.backend.calls)

    def test_a_second_pass_changes_nothing(self):
        self.write([self.razer("seat1")])
        known = broker.cycle(self.backend, "seat1", "seat1", (), None, {})
        count = len(self.backend.calls)

        again = broker.cycle(self.backend, "seat1", "seat1", (), None, known)

        self.assertEqual(again, known)
        self.assertEqual(len(self.backend.calls), count)

    def test_unplugging_detaches_it(self):
        self.write([self.razer("seat1", interfaces=[0])])
        known = broker.cycle(self.backend, "seat1", "seat1", (), None, {})

        self.unplug()
        known = broker.cycle(self.backend, "seat1", "seat1", (), None, known)

        self.assertEqual(known, {})
        self.assertEqual(self.backend.of("detach"), [("detach", "seat1", "event7")])
        self.assertEqual(self.backend.nodes["seat1"], set())

    def test_removing_the_assignment_detaches_it(self):
        self.write([self.razer("seat1")])
        known = broker.cycle(self.backend, "seat1", "seat1", (), None, {})
        self.assertEqual(len(known), 4)

        self.write([])
        known = broker.cycle(self.backend, "seat1", "seat1", (), None, known)

        self.assertEqual(known, {})
        self.assertEqual(len(self.backend.of("detach")), 4)

    def test_reassigning_to_another_seat_moves_it(self):
        self.write([self.razer("seat1", interfaces=[0])])
        known = broker.cycle(self.backend, "seat1", "seat1", (), None, {})

        self.write([self.razer("seat2", interfaces=[0])])
        known = broker.cycle(self.backend, "seat1", "seat1", (), None, known)
        other = broker.cycle(self.backend, "seat2", "seat2", (), None, {})

        self.assertEqual(known, {})
        self.assertEqual(sorted(other), ["event7"])

    def test_a_broker_restart_keeps_it(self):
        self.write([self.razer("seat1", interfaces=[0])])
        broker.cycle(self.backend, "seat1", "seat1", (), None, {})
        self.assertEqual(self.backend.nodes["seat1"], {"event7"})

        # The broker comes back: state file and attachment survive, `known`
        # does not. The name carries no "(seat1)", which is what makes any
        # other device look like an orphan.
        removed = broker.remove_orphans(self.backend, "seat1", "seat1")

        self.assertEqual(removed, 0)
        self.assertEqual(self.backend.of("detach"), [])
        self.assertEqual(self.backend.nodes["seat1"], {"event7"})

    def test_an_assignment_removed_while_the_broker_was_down_is_cleaned_up(self):
        self.write([self.razer("seat1", interfaces=[0])])
        broker.cycle(self.backend, "seat1", "seat1", (), None, {})

        self.write([])
        # With the name check off, as `--tag ""` does, only the memory can tell.
        removed = broker.remove_orphans(self.backend, "seat1", None)

        self.assertEqual(removed, 1)
        self.assertEqual(self.backend.nodes["seat1"], set())
        # The remove event went out too, because the state file remembered it.
        self.assertIn(("run", "seat1", broker.FAKEUDEV), self.backend.calls)

    def test_a_device_unplugged_while_the_broker_was_down_is_cleaned_up(self):
        self.write([self.razer("seat1", interfaces=[0])])
        broker.cycle(self.backend, "seat1", "seat1", (), None, {})

        self.unplug()
        removed = broker.remove_orphans(self.backend, "seat1", "seat1")

        self.assertEqual(removed, 1)
        self.assertEqual(self.backend.nodes["seat1"], set())


if __name__ == "__main__":
    unittest.main()
