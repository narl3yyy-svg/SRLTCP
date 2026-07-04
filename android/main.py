"""
SRLTCP - Android App
Main entry point for the Kivy application
"""

import os
import sys
import threading
import socket

import kivy
from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

kivy.require('2.2.0')

# Allow running directly from android/ folder during development.
# On Android the package is installed properly via requirements.source.srltcp
if 'srltcp' not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# You can now import from srltcp normally:
# from srltcp.transports.tcp import TCPTransport
# import srltcp


class SRLTCPApp(App):
    def build(self):
        self.title = 'SRLTCP'
        layout = BoxLayout(orientation='vertical', padding=10, spacing=10)
        
        # Title
        title_label = Label(text='SRLTCP Client', font_size='24sp', size_hint_y=0.1)
        layout.add_widget(title_label)
        
        # Server IP input
        ip_layout = BoxLayout(size_hint_y=0.1, spacing=10)
        ip_layout.add_widget(Label(text='Server IP:', size_hint_x=0.3))
        self.ip_input = TextInput(text='127.0.0.1', multiline=False, size_hint_x=0.7)
        ip_layout.add_widget(self.ip_input)
        layout.add_widget(ip_layout)
        
        # Port input
        port_layout = BoxLayout(size_hint_y=0.1, spacing=10)
        port_layout.add_widget(Label(text='Port:', size_hint_x=0.3))
        self.port_input = TextInput(text='5000', multiline=False, size_hint_x=0.7)
        port_layout.add_widget(self.port_input)
        layout.add_widget(port_layout)
        
        # Connect button
        self.connect_btn = Button(text='Connect', size_hint_y=0.1)
        self.connect_btn.bind(on_press=self.connect_to_server)
        layout.add_widget(self.connect_btn)
        
        # Status label
        self.status_label = Label(text='Disconnected', size_hint_y=0.05, color=(1, 1, 0, 1))
        layout.add_widget(self.status_label)
        
        # Message input
        msg_layout = BoxLayout(size_hint_y=0.1, spacing=10)
        self.msg_input = TextInput(text='Hello Server!', multiline=False, size_hint_x=0.7)
        msg_layout.add_widget(self.msg_input)
        send_btn = Button(text='Send', size_hint_x=0.3)
        send_btn.bind(on_press=self.send_message)
        msg_layout.add_widget(send_btn)
        layout.add_widget(msg_layout)
        
        # Log area
        self.log_text = TextInput(text='', readonly=True, multiline=True)
        scroll = ScrollView()
        scroll.add_widget(self.log_text)
        layout.add_widget(scroll)
        
        return layout
    
    def connect_to_server(self, instance):
        self.log_text.text += "Connecting...\n"
        self.status_label.text = 'Connecting...'
        self.status_label.color = (1, 1, 0, 1)
        # TODO: Add real connection logic using srltcp here
        Clock.schedule_once(lambda dt: self.update_status('Connected'), 1)
    
    def send_message(self, instance):
        msg = self.msg_input.text
        if msg:
            self.log_text.text += f"Sending: {msg}\n"
            # TODO: Add real send logic using srltcp here
            self.msg_input.text = ''
    
    def update_status(self, status):
        self.status_label.text = status
        self.status_label.color = (0, 1, 0, 1)
        self.log_text.text += f"{status}\n"


if __name__ == '__main__':
    SRLTCPApp().run()
