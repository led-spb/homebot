#!/usr/bin/python
# -*- coding: utf-8 -*-
import re
import os
import os.path
import logging
import shlex
import argparse
import subprocess
import time
import datetime
import urlparse
import paho_async.client as mqtt
from cStringIO import StringIO
from pytelegram_async.bot import Bot, BotRequestHandler, PatternMessageHandler, MessageHandler
from pytelegram_async.entity import *
from tornado.ioloop import IOLoop, PeriodicCallback
from jinja2 import Environment
from paho.mqtt.client import topic_matches_sub
import humanize
import telebots
from sensors import Sensor


class HomeBotHandler(BotRequestHandler, mqtt.TornadoMqttClient):
    def __init__(self, ioloop, admins, mqtt_url, sensors=None, extra_cmds=None):
        BotRequestHandler.__init__(self)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.ioloop = ioloop

        self.sensors = [self.build_sensor_from_url(url, admins) for url in sensors or []]
        self.background_processes = []

        self.trigger_gap = 300

        self.jinja = Environment()
        self.jinja.filters['human_date'] = self.human_date
        self.sensor_template = self.jinja.from_string(
            "<b>{{sensor.name}}</b>: {{ sensor.state_text() }} {{ sensor.changed | human_date }}"
        )

        self.sensor_full_template = self.jinja.from_string(
            "<b>name</b>: {{sensor.name}}\n"
            "<b>type</b>: {{sensor.type}}\n"
            "<b>state</b>: {{sensor.state_text()}}\n"
            "<b>changed</b>: {{ sensor.changed | human_date }}\n"
            "<b>triggered</b>: {{ sensor.triggered | human_date }}"
        )

        host = mqtt_url.hostname
        port = mqtt_url.port if mqtt_url.port is not None else 1883

        self.logger.info("Trying connect to MQTT broker at %s:%d" % (host, port))

        self.subscribe = False
        mqtt.TornadoMqttClient.__init__(
            self, ioloop=ioloop, host=mqtt_url.hostname,
            port=mqtt_url.port if mqtt_url.port is not None else 1883,
            username=mqtt_url.username, password=mqtt_url.password
        )
        self.version = telebots.version
        self.shell_commands = extra_cmds or {}
        self._periodic_task = PeriodicCallback(callback=self.periodic_handler, callback_time=1000)
        pass

    def periodic_handler(self):
        # check background processes for finished and send notification
        for info in list(self.background_processes):
            process, chat, command = info
            if process.poll() is not None:
                self.background_processes.remove(info)

                logging.info('Command "%s" is ends', command)
                (result, _) = process.communicate()
                logging.debug(result)
                if result is not None and result.strip() != "":
                    self.bot.send_message(
                        to=chat['id'], message=result.strip(), parse_mode='HTML'
                    )
        pass

    def start(self):
        super(HomeBotHandler, self).start()
        self._periodic_task.start()

    def build_sensor_from_url(self, url, admins):
        sensor = Sensor.from_url(url, admins)
        if sensor.type == "camera":
            sensor.on_changed = self.event_camera
        elif sensor.type == "notify":
            sensor.on_changed = self.event_notify
        else:
            sensor.on_changed = self.event_sensor
        return sensor

    def sensor_by_name(self, name):
        for sensor in self.sensors:
            if sensor.name == name:
                return sensor
        return None

    @staticmethod
    def human_date(value):
        if isinstance(value, float) or isinstance(value, int):
            value = datetime.datetime.fromtimestamp(value)
        return humanize.naturaltime(value)

    def on_mqtt_connect(self, client, obj, flags, rc):
        self.logger.info("MQTT broker: %s", mqtt.connack_string(rc))
        if rc == 0:
            # Subscribe sensors topics
            for sensor in self.sensors:
                client.subscribe(sensor.topic)
        pass

    def on_mqtt_message(self, client, obj, message):
        if message.retain:
            return
        self.logger.info("topic %s, payload: %s" % (
            message.topic,
            "[binary]" if len(message.payload) > 10 else message.payload
        ))
        for sensor in self.sensors:
            if topic_matches_sub(sensor.topic, message.topic):
                return sensor.process(message.topic, message.payload)
        pass

    @PatternMessageHandler("/video( .*)?", authorized=True)
    def cmd_video(self, chat, text, message_id, is_callback):
        params = text.split()
        video = params[1] if len(params) > 1 else None

        if video is None:
            files = sorted([x for x in os.listdir('/home/hub/motion/storage')
                            if re.match(r'\d{8}_\d{6}\.mp4', x)], reverse=True)

            buttons = [{
                'callback_data': '/video '+fname,
                'text': re.sub(r'^\d{8}_(\d{2})(\d{2}).*$', '\\1:\\2', fname)
            } for fname in files]
            max_btn_inrow = 6
            keyboard = [
                x for x in [
                    buttons[i*max_btn_inrow:(i+1)*max_btn_inrow] for i in range(len(buttons)/max_btn_inrow+1)
                ] if len(x) > 0
            ]
            self.bot.send_message(
                to=chat.get('id'),
                message='which video?',
                reply_markup={'inline_keyboard': keyboard}
            )
        else:
            caption = re.sub(r'^\d{8}_(\d{2})(\d{2}).*$', '\\1:\\2', video)
            self.bot.send_message(
                to=chat.get('id'),
                message=Video(
                    video=File('video.mp4', open('/home/hub/motion/storage/'+video, 'rb'), 'video/mp4'),
                    caption=caption
                )
            )
        return True

    @PatternMessageHandler("/status", authorized=True)
    def cmd_status(self, chat):
        self.notify_sensor(chat['id'])
        return True

    @PatternMessageHandler("/sensor( .*)?", authorized=True)
    def cmd_sensor(self, chat, text, message_id, is_callback):
        params = text.split()

        def show_menu():
            buttons = [
                {'callback_data': '/sensor %s' % item.name, 'text': item.name}
                for item in self.sensors
            ]
            message_text = 'Which sensor?'
            message_params = {
                'to': chat['id'],
                'reply_markup': {'inline_keyboard': [buttons]},
                'parse_mode': 'HTML',
                'message': message_text,
                'text': message_text,
                'message_id': message_id
            }
            send_method = self.bot.send_message
            if is_callback:
                send_method = self.bot.edit_message_text
            send_method(**message_params)
            pass

        def show_sensor_menu(sensor):
            buttons = [
                {'callback_data': '/sensor %s 1' % sensor.name, 'text': 'Subscribe'},
                {'callback_data': '/sensor %s 0' % sensor.name, 'text': 'Unsubscribe'}
            ]
            message_text = self.sensor_full_template.render(sensor=sensor)
            message_params = {
                'to': chat['id'],
                'reply_markup': {
                    'inline_keyboard': [
                        buttons,
                        [{'callback_data': '/sensor', 'text': 'Back'}]
                    ]
                 },
                'parse_mode': 'HTML',
                'message': message_text,
                'text': message_text,
                'message_id': message_id
            }
            send_method = self.bot.send_message
            if is_callback:
                send_method = self.bot.edit_message_text
            send_method(**message_params)
            pass

        if len(params) == 1:
            show_menu()
        elif len(params) == 2:
            sensor = self.sensor_by_name(params[1])
            if sensor is None:
                show_menu()
            else:
                show_sensor_menu(sensor)
            pass
        if len(params) == 3:
            sensor = self.sensor_by_name(params[1])
            if sensor is None:
                show_menu()
                return True
            if int(params[2]) > 0:
                sensor.add_subscription(chat['id'])
            else:
                sensor.remove_subscription(chat['id'])

            message_text = 'Sensor <b>%s</b> changed' % sensor.name
            message_params = {
                'to': chat['id'],
                'parse_mode': 'HTML',
                'message': message_text,
                'text': message_text,
                'message_id': message_id
            }
            send_method = self.bot.send_message
            if is_callback:
                send_method = self.bot.edit_message_text
            send_method(**message_params)
        return True

    def notify_sensor(self, chat_id, sensor=None):
        messages = [
            self.sensor_template.render(sensor=item)
            for item in self.sensors
            if (sensor is None or item == sensor) and not item.is_dummy
        ]
        return self.bot.send_message(to=chat_id, message="\n".join(messages), parse_mode='HTML')

    @PatternMessageHandler(r'/camera (\S+)', authorized=True)
    def cmd_camera(self, chat, text):
        params = text.split()
        if len(params) != 2:
            return
        camera = self.sensor_by_name(params[1])
        if camera is not None and chat['id'] not in camera.one_time_sub:
            camera.one_time_sub.append(chat['id'])
        return True

    @MessageHandler(authorized=True)  
    def cmd_shell(self, chat, text):
        if text in self.shell_commands:
            command = self.shell_commands[text]
            logging.debug("Executing shell command: %s", command)
            process = subprocess.Popen(
                shlex.split(command), stderr=subprocess.STDOUT, stdout=subprocess.PIPE
            )
            self.background_processes.append((process, chat, command))
            return True
        return False

    def event_camera(self, camera):
        event_type = camera.event_type
        self.logger.info("Camera sensor %s triggered for event %s", camera.name, camera.event_type)

        # Photo events send only subscribers
        if event_type == 'photo':
            markup = {
                    'inline_keyboard': [[{
                        'text': 'Subscribe',
                        'callback_data': '/camera %s' % camera.name
                    }]]
            }
            for chat_id in camera.subscriptions:
                self.bot.send_message(
                    to=chat_id,
                    message=Photo(
                        photo=File('image.jpg', StringIO(camera.state), 'image/jpeg'),
                        caption='camera#%s' % camera.name
                    ),
                    reply_markup=markup
                )
            return None

        if event_type == 'videom':
            for chat_id in camera.subscriptions:
                self.bot.send_message(
                    to=chat_id,
                    message=Video(
                        video=File('camera_%s.mp4' % camera.name, StringIO(camera.state), 'video/mp4')
                    )
                )

        if event_type == 'video':
            for chat_id in camera.one_time_sub:
                self.bot.send_message(
                    to=chat_id,
                    message=Video(
                        video=File('camera_%s.mp4' % camera.name, StringIO(camera.state), 'video/mp4')
                    )
                )
            camera.one_time_sub = []
        pass

    def event_notify(self, sensor):
        self.logger.info("Notify sensor %s triggered", sensor.name)
        for chat_id in sensor.subscriptions:
            self.bot.send_message(
                to=chat_id,
                message="<b>%s</b>: %s" % (sensor.name, sensor.state),
                parse_mode='HTML'
            )
        pass

    def event_sensor(self, sensor):
        self.logger.info("Sensor %s changed to %d", sensor.name, sensor.state)
        status = sensor.state
        now = time.time()

        if status > 0 and (now-sensor.triggered) > self.trigger_gap:
            sensor.triggered = now
            for chat_id in sensor.subscriptions:
                self.notify_sensor(chat_id, sensor)
        pass


def main():

    class LoadFromFile(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            with values as f:
                parser.parse_args(shlex.split(f.read()), namespace)

    parser = argparse.ArgumentParser(fromfile_prefix_chars='@')

    basic = parser.add_argument_group('basic', 'Basic parameters')
    basic.add_argument("-c", "--config", type=open, action=LoadFromFile, help="Load config from file")
    basic.add_argument("-u", "--url", default="mqtt://localhost:1883", type=urlparse.urlparse,
                       help="MQTT Broker address host:port")
    basic.add_argument("--token", help="Telegram API bot token")
    basic.add_argument("--admin", nargs="+", help="Bot admin", type=int, dest="admins")
    basic.add_argument("--extra", help="Run process on command /command:shell_exe", nargs="*", dest="extra")
    basic.add_argument("--proxy")
    basic.add_argument("--logfile", help="Logging into file")
    basic.add_argument("-v", action="store_true", default=False, help="Verbose logging", dest="verbose")

    status = parser.add_argument_group('status', 'Home state parameters')
    status.add_argument("--sensors", nargs="*", help="Sensor in URL format: type://name[!]@mqtt_topic")

    args = parser.parse_args()

    # configure logging
    logging.basicConfig(
        format="[%(asctime)s]\t[%(levelname)s]\t[%(name)s]\t%(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
        filename=args.logfile
    )
    logging.info("Starting telegram bot")
    ioloop = IOLoop.instance()
    bot = Bot(args.token, args.admins, proxy=args.proxy, ioloop=ioloop)

    cmds = {x.split(':', 1)[0]: x.split(':', 1)[1] for x in args.extra}
    handler = HomeBotHandler(ioloop=ioloop, admins=bot.admins, mqtt_url=args.url, sensors=args.sensors, extra_cmds=cmds)
    bot.add_handler(handler)
    bot.loop_start()
    handler.start()
    try:
        ioloop.start()
    except KeyboardInterrupt:
        ioloop.stop()
    pass


if __name__ == '__main__':
    main()
