#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import os.path
import logging
import argparse
import time
import json
import urlparse
from telebot import Bot, BotRequestHandler
import paho.mqtt.client as mqtt
from cStringIO import StringIO
import re
from telebot import Bot, BotRequestHandler, authorized
from asyncmqtt import TornadoMqttClient
from tornado import gen
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.httpclient import AsyncHTTPClient


class HomeBotHandler(BotRequestHandler, TornadoMqttClient):
    def __init__(self, ioloop, mqtt_url, sensors=None, cameras=None):
        self.logger  = logging.getLogger(self.__class__.__name__)
        self.ioloop  = ioloop
        self.sensors = sensors or []
        self.cameras = cameras or []
        self.event_gap = 300
        self.events = {}
        self.http_client = AsyncHTTPClient()

        host = mqtt_url.hostname
        port = mqtt_url.port if mqtt_url is not None else 1883

        self.logger.info("Trying connect to MQTT broker at %s:%d" %
                         (host, port))

        self.subscribe = False
        TornadoMqttClient.__init__(self, 
             ioloop = ioloop,
             host = mqtt_url.hostname, 
             port = mqtt_url.port if mqtt_url.port!=None else 1883,
             username = mqtt_url.username,
             password = mqtt_url.password
        )
        pass

    def on_mqtt_connect(self, client, obj, flags, rc):
        self.logger.info("MQTT broker: %s", mqtt.connack_string(rc))
        if rc == 0:
            topics = ["home/notify"] + \
                     ["home/sensor/%s" % x for x in self.sensors] + \
                     ["home/camera/%s/#" % x for x in self.cameras]
            for topic in topics:
                self.logger.debug("Subscribe for topic %s" % topic)
                client.subscribe(topic)
        pass

    def on_mqtt_message(self, client, obj, msg):
        if msg.retain:
            return

        self.logger.info("topic %s, payload: %s" % (
            msg.topic,
            "[binary]" if len(msg.payload) > 10 else msg.payload
        ))
        path = msg.topic.split('/')[1:]
        event = path[0]
        self.logger.debug("Event %s path: %s" % (event, repr(path[1:])))

        self.exec_event(event, path[1:], msg.payload)
        pass

    def exec_event(self, name, path, payload):
        if hasattr(self, "event_"+name):
            handler = getattr(self, "event_"+name)
            handler(path, payload)
        pass

    @authorized
    def cmd_photo(self, message=None):
        self.http_client.fetch("http://127.0.0.1:8082/0/action/snapshot")
        return None

    @authorized
    def cmd_sub(self, message=None):
        args = message['text'].split()

        if len(args) < 1 or args[0].lower() != 'off':
            self.subscribe = True
        else:
            self.subscribe = False
        return None

    """
    @authorized
    def cmd_stat(self, *args):
        template = '{% for item in states.sensor %}' \
                   '{% if item.state!=\'unknown\' %}' \
                   '{{ item.name }} is {{item.state_with_unit}}\n' \
                   '{% endif %}{% endfor %}'
        return requests.post('http://127.0.0.1:8123/api/template',
                             data=json.dumps({'template': template})).text

    @authorized
    def cmd_door(self, *args):
        template = 'Door is {{ \'closed\' if states.binary_sensor.door.state == \'off\' else \'open\' }} for {{ relative_time(states.binary_sensor.door.last_changed) }} ({{ as_timestamp(states.binary_sensor.door.last_changed) | timestamp_custom() }})'
        return requests.post('http://127.0.0.1:8123/api/template',
                             data=json.dumps({'template': template})).text
    """

    @authorized
    def cmd_video(self, message=None):
        params = message['text'].split()
        video = params[1] if len(params)>1 else None

        if video is None:
            files = sorted([x for x in os.listdir('/home/hub/motion')
                            if re.match('\d{8}_\d{6}\.mp4', x)], reverse=True)

            buttons = [{
                'callback_data': '/video '+fname,
                'text': re.sub('^\d{8}_(\d{2})(\d{2}).*$', '\\1:\\2', fname)
            } for fname in files]
            keyboard = [
                x for x in [
                    buttons[i*7:(i+1)*7] for i in range(len(buttons)/7+1)
                ] if len(x) > 0
            ]
            return self.bot.send_message(
                       to = message['chat']['id'],
                       text='which video?',
                       markup={'inline_keyboard': keyboard}
            )

        caption = re.sub('^\d{8}_(\d{2})(\d{2}).*$', '\\1:\\2', video)
        return self.bot.send_message(
                   to = message['chat']['id'],
                   video = ('video.mp4', open('/home/hub/motion/'+video, 'rb'), 'video/mp4'),
                   extra = {'caption': caption}
                   
        )
    
    def event_camera(self, path, payload):
        cam_no = path[0]
        event_type = path[1]
        if event_type == 'photo':
            markup = None
            if not self.subscribe:
                markup= {
                        'inline_keyboard': [[{
                            'text': 'Subscribe',
                            'callback_data': '/sub'
                        }]]
                }

            for chat_id in self.bot.admins:
                self.bot.send_message( 
                   to=chat_id,
                   photo=('image.jpg', StringIO(payload), 'image/jpeg'),
                   extra={'caption': 'camera#%s' % cam_no},
                   markup=markup
                )
            return None

        if event_type == 'videom' or \
           (self.subscribe and event_type == 'video'):
            self.subscribe = False
            for chat_id in self.bot.admins:
                self.bot.send_message(
                   to=chat_id, video= ('video.mp4', StringIO(payload), 'video/mp4')
                )
        pass
    

    def event_notify(self, path, payload):
        self.logger.info("Event notify")
        for admin in self.bot.admins:
            self.bot.send_message(
                to=admin, text=payload
            );
        pass

    def event_sensor(self, path, payload):
        sensor = path[0]
        value = payload
        now = time.time()

        if int(value) > 0 and \
           (sensor not in self.events or
           (now-self.events[sensor]) > self.event_gap):
            self.events[sensor] = now
            for admin in self.bot:
                self.bot.send_message(
                   to=admin, text="%s: alert %s" % (sensor, time.strftime("%d.%m %H:%M"))
                )
        pass


def main():
    logging.getLogger("requests").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    class LoadFromFile(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            with values as f:
                parser.parse_args(f.read().split(), namespace)

    parser = argparse.ArgumentParser(fromfile_prefix_chars='@')

    basic = parser.add_argument_group('basic', 'Basic parameters')
    basic.add_argument("-c", "--config", type=open,
                       action=LoadFromFile,
                       help="Load config from file")
    basic.add_argument("-u", "--url",
                       default="localhost:1883",
                       type=urlparse.urlparse,
                       help="MQTT Broker address host:port")
    basic.add_argument("--token", help="Telegram API bot token")
    basic.add_argument("--admin", nargs="+", help="Bot admin",
                       type=int, dest="admins")
    basic.add_argument("--proxy")
    basic.add_argument("--logfile", help="Logging into file")
    basic.add_argument("-v", action="store_true",
                       default=False, help="Verbose logging", dest="verbose")

    status = parser.add_argument_group('status', 'Home state parameters')
    status.add_argument("--sensors", nargs="*",
                        help="Notify state of this sensors", type=str)
    status.add_argument("--cameras", nargs="*",
                        help="Notify state of this camera", type=str)

    args = parser.parse_args()

    # configure logging
    logging.basicConfig(
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
        filename=args.logfile
    )
    logging.info("Starting telegram bot")
    ioloop = IOLoop.instance()

    handler = HomeBotHandler(
        ioloop,
        args.url,
        sensors=args.sensors,
        cameras=args.cameras
    )
    bot = Bot(args.token, args.admins, proxy=args.proxy, ioloop=ioloop)
    # Default handler
    bot.addHandler(handler)
    bot.loop_start()
    handler.start()
    try:
       ioloop.start()
    except KeyboardInterrupt, e:
       ioloop.stop()
    finally:
       pass
    pass


if __name__ == '__main__':
    main()