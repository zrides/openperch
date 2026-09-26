"""The account's camera, as the hub shows it: a small, cached, credential-free summary."""
from __future__ import annotations

import threading
import time

from .kiwibit_api import KiwibitClient, load_config


# The camera describes its own settings (name, type, value, options). These
# groups and labels make them readable; anything unlisted appears under Other.
GROUPS = [
    ('Motion and recording', [
        ('pirSwitch', 'Motion detection', 'Record a clip when something moves at the feeder'),
        ('pirSensitivity', 'Motion sensitivity', ''),
        ('pirRecordTime', 'Clip length', 'Recording time after motion'),
        ('pirCooldownSwitch', 'Pause between clips', 'Wait before recording again'),
        ('pirCooldownTime', 'Pause length', ''),
        ('offZoneRecordingSwitch', 'Record outside the activity zone', ''),
        ('enableAllRecordMotionsInDark', 'Record all motion in the dark', ''),
        ('pirRecordingImageQuality', 'Recording image quality', ''),
        ('timedDormancySwitch', 'Scheduled sleep', 'Sleep on a schedule to save battery'),
    ]),
    ('Video', [
        ('videoResolution', 'Recording resolution', ''),
        ('liveStreamCodec', 'Live stream codec', ''),
        ('videoFlipSwitch', 'Flip image', ''),
        ('videoAntiFlickerSwitch', 'Anti-flicker', ''),
        ('videoAntiFlickerFrequency', 'Anti-flicker frequency', ''),
        ('timeWatermarkSwitch', 'Time watermark', ''),
        ('video12HourSwitch', '12-hour clock in watermark', ''),
        ('logoWatermarkSwitch', 'Logo watermark', ''),
    ]),
    ('Night vision and light', [
        ('nightVisionSwitch', 'Night vision', ''),
        ('nightVisionMode', 'Night vision mode', ''),
        ('nightVisionSensitivity', 'Night vision sensitivity', ''),
        ('recLampSwitch', 'Recording light', 'Light on the camera while recording'),
        ('overallLightIntensity', 'Light intensity', ''),
    ]),
    ('Sound and alarm', [
        ('liveAudioSwitch', 'Audio in live view', ''),
        ('recordingAudioSwitch', 'Audio in recordings', ''),
        ('voiceVolume', 'Speaker volume', ''),
        ('voiceLanguage', 'Voice prompt language', ''),
        ('alarmVolume', 'Alarm volume', ''),
        ('alarmDuration', 'Alarm duration', ''),
        ('nuisanceAnimalAlarmSwitch', 'Alarm for nuisance animals', ''),
        ('nuisanceAnimalAlarmDuration', 'Nuisance animal alarm duration', ''),
    ]),
    ('AI and notifications', [
        ('detectBirdAi', 'Cloud bird identification', "The camera service's own species labels"),
        ('detectNuisanceAnimalAi', 'Cloud nuisance animal detection', ''),
        ('pushSwitch', 'Phone notifications', ''),
        ('messageMergeSwitch', 'Group notifications', ''),
        ('motionAlertSwitch', 'Motion alerts', ''),
        ('criticalAlerts', 'Critical alerts', ''),
    ]),
    ('Power and network', [
        ('powerSource', 'Power source', ''),
        ('chargeAutoPowerOnSwitch', 'Power on when charged', ''),
        ('chargeAutoPowerOnCapacity', 'Power on at charge', ''),
        ('wifiPowerMode', 'Wi-Fi power mode', ''),
        ('otaAutoUpgrade', 'Automatic firmware updates', ''),
    ]),
    ('Device', [
        ('deviceName', 'Name', ''),
        ('location', 'Location', ''),
        ('home', 'Home', ''),
        ('timeZone', 'Time zone', ''),
    ]),
]
OPTION_LABELS = {
    'high': 'High', 'mid': 'Medium', 'medium': 'Medium', 'low': 'Low', 'auto': 'Auto', '4k': '4K',
    'h264': 'H.264', 'h265': 'H.265', 'infrared': 'Infrared', 'solar_panel': 'Solar panel', 'plug_in': 'Plug-in',
    'battery_power_only': 'Battery only', 'default': 'Default', 'turbo': 'Turbo',
    'de': 'German', 'en': 'English', 'fr': 'French', 'it': 'Italian', 'pt': 'Portuguese', 'es': 'Spanish',
}


def option_label(value):
    if isinstance(value, str) and value.endswith('pct') and value[:-3].isdigit():
        return value[:-3] + '%'
    return OPTION_LABELS.get(value, str(value))


def describe_attribute(attribute, label=None, description=''):
    """A display-ready setting. OBJECT_ENUM values and options are {id, text} objects."""
    kind = attribute.get('type')
    if kind == 'OBJECT_ENUM':
        def key(item):
            return str(item.get('id')) if isinstance(item, dict) else str(item)

        def text(item):
            return str(item.get('text') or item.get('id')) if isinstance(item, dict) else str(item)
        raw = attribute.get('value')
        value = key(raw) if raw is not None else None
        options = [{'value': key(o), 'label': text(o), 'disabled': False} for o in attribute.get('options') or []]
        if raw is not None and all(o['value'] != value for o in options):
            options.append({'value': value, 'label': text(raw), 'disabled': False})
    else:
        value = attribute.get('value')
        offered = [o for o in attribute.get('options') or [] if isinstance(o, (str, int, float))]
        unavailable = [o for o in attribute.get('disabledOptions') or [] if isinstance(o, (str, int, float))]
        options = [{'value': o, 'label': option_label(o), 'disabled': o in unavailable} for o in offered]
        options += [{'value': o, 'label': option_label(o), 'disabled': True} for o in unavailable if o not in offered]
    return {
        'name': attribute['name'], 'label': label or attribute['name'], 'description': description,
        'type': 'ENUM' if kind == 'OBJECT_ENUM' else kind, 'value': value,
        'disabled': bool(attribute.get('disabled')), 'options': options,
    }


def group_attributes(attributes):
    by_name = {}
    for attribute in attributes:
        if isinstance(attribute, dict) and attribute.get('name') and attribute['name'] not in by_name:
            by_name[attribute['name']] = attribute  # the camera may list a setting twice
    groups = []
    for title, entries in GROUPS:
        settings = [describe_attribute(by_name.pop(name), label, description)
                    for name, label, description in entries if name in by_name]
        if settings:
            groups.append({'title': title, 'settings': settings})
    if by_name:
        groups.append({'title': 'Other', 'settings': [describe_attribute(a) for a in by_name.values()]})
    return groups


class CameraService:
    def __init__(self, account=None, client=None, ttl=60):
        self._account = account
        self._client = client
        self.ttl = ttl
        self.lock = threading.Lock()
        self.cached = None
        self.fetched = 0.0
        self.settings_cache = None
        self.settings_fetched = 0.0

    def account(self):
        if self._account is None:
            self._account = load_config()
        return self._account

    def client(self):
        if self._client is None:
            self._client = KiwibitClient(self.account())
        return self._client

    def token(self):
        return self.account()['token'].removeprefix('Bearer ').strip()

    def device(self, refresh=False):
        """The preferred camera's raw record: the configured default, else the first."""
        with self.lock:
            if refresh or self.cached is None or time.monotonic() - self.fetched > self.ttl:
                devices = self.client().devices()
                if not devices:
                    raise LookupError('No cameras are registered on this account')
                preferred = self.account().get('defaultSerialNumber')
                self.cached = next((d for d in devices if d.get('serialNumber') == preferred), devices[0])
                self.fetched = time.monotonic()
            return self.cached

    def summary(self, refresh=False):
        device = self.device(refresh)
        return {
            'serial': device.get('serialNumber'),
            'name': device.get('deviceName') or 'Camera',
            'model': device.get('displayModelNo') or device.get('modelNo'),
            'model_number': device.get('modelNo'),
            'online': bool(device.get('online')),
            'awake': bool(device.get('awake')),
            'battery': device.get('batteryLevel'),
            'firmware': device.get('firmwareId'),
            'recording_resolution': device.get('recResolution'),
            'can_watch_live': device.get('canWatchLive', True) is not False,
        }

    def settings(self, refresh=False):
        """Current camera settings (read-only), cached like the summary.

        Setting *changes* need the phone app's write calls, which have not been
        captured yet, so nothing here modifies the camera.
        """
        with self.lock:
            if not refresh and self.settings_cache and time.monotonic() - self.settings_fetched <= self.ttl:
                return self.settings_cache
        device = self.device(refresh)
        serial = device['serialNumber']
        client = self.client()

        def optional(call, *args):
            try:
                return call(*args) or {}
            except Exception:  # one unavailable section must not hide the rest
                return {}

        attributes = optional(client.device_attributes, serial).get('modifiableAttributes') or []
        bird_ai = optional(client.bird_ai_setting, serial)
        zones = optional(client.activity_zones, serial)
        detail = optional(client.device, serial)
        result = {
            'status': {
                'battery': device.get('batteryLevel'),
                'wifi_dbm': detail.get('signalStrength') if detail.get('signalStrength') is not None else detail.get('wifiPowerDbm'),
                'wifi_level': detail.get('signalLevel'),
                'firmware': device.get('firmwareId'),
                'online': bool(device.get('online')),
                'awake': bool(device.get('awake')),
            },
            'groups': group_attributes(attributes),
            'bird_ai': {'analyze': bird_ai.get('aiAnalyzeSwitch'), 'notify': bird_ai.get('aiNotifySwitch')},
            'activity_zones': len(zones.get('list') or []) if zones else None,
            'writable': False,
        }
        with self.lock:
            self.settings_cache, self.settings_fetched = result, time.monotonic()
        return result

    def wake(self):
        device = self.device()
        self.client().wake(device['serialNumber'])
        with self.lock:
            self.fetched = 0.0  # re-read awake state on the next request
