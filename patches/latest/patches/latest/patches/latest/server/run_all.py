# run_all.py - start the three CSXY simulator services in one console.
#
#   HTTP    8000  update server (launcher: server3.txt / patch_list1.txt)
#   Login   20560 login-app listener (local simulator)
#   BaseApp 28000 world listener (local simulator with player/NPC AOI)
#
# Env overrides:
#   LOGIN_HOST     address embedded in server3.txt (default 127.0.0.1).
#                  For a VM client use the host's IP, e.g. 192.168.233.1.
#   REPLY=on|off   placeholder replies on login (default on)
#   HTTP_PORT LOGIN_PORT BASEAPP_PORT  port overrides
#
# Raw transport data is archived under evidence/{login_frames,baseapp_frames}.
#
import datetime, logging, os, sys, threading

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(BASE, "logs")
os.makedirs(LOGDIR, exist_ok=True)
LOG = os.path.join(LOGDIR, "run_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG, encoding="utf-8")],
)
log = logging.getLogger("run_all")

sys.path.insert(0, BASE)

import http_update_server
import login_server
import baseapp_server
import udp_reply_server
import udp_baseapp_server
from csxy_protocol import check_resource_key

STOP = threading.Event()
READY = {name: threading.Event() for name in
         ('http', 'login-tcp', 'login-udp', 'baseapp-tcp', 'baseapp-udp')}


def run_http():
    log.info("HTTP : starting on %s:%d (LOGIN_HOST=%s)" %
             (http_update_server.HOST, http_update_server.HTTP_PORT, http_update_server.LOGIN_HOST))
    srv = http_update_server.ThreadingHTTPServer(
        (http_update_server.HOST, http_update_server.HTTP_PORT), http_update_server.Handler)
    READY['http'].set()
    srv.serve_forever()


def run_login():
    login_server.serve(done=STOP, reply=False,
                       on_ready=lambda port: READY['login-tcp'].set())


def run_baseapp():
    baseapp_server.serve(done=STOP, reply=False,
                         on_ready=lambda port: READY['baseapp-tcp'].set())

def run_udp_login():
    # The CSXY client sends its LoginApp request as UDP datagrams.  TCP 20560
    # is retained for raw capture, while this listener handles the real client
    # transport.  REPLY_MODE is configurable; login_ok is the closest known
    # Mercury response and every datagram is archived before replying.
    udp_reply_server.main(done=STOP, on_ready=lambda port: READY['login-udp'].set())


def run_udp_baseapp():
    udp_baseapp_server.main(done=STOP,
                           on_ready=lambda port: READY['baseapp-udp'].set())


def guarded(name, target):
    try:
        target()
    except Exception:
        log.exception('Service %s failed; stack will stop', name)
        STOP.set()


def main():
    if udp_reply_server.MODE != 'none':
        check_resource_key()
    import hot_control
    from startup_repair import repair
    hot_control.ensure_files()
    repair()  # __main__ holds service_lease; never runs during import/preflight.
    log.info("=" * 62)
    log.info("CSXY simulator stack starting - log: %s" % LOG)
    log.info("  HTTP    %s:%s  (update/launcher)" % (http_update_server.HOST, http_update_server.HTTP_PORT))
    log.info('  Login UDP: mode=%s, 20-byte success record', udp_reply_server.MODE)
    log.info('  BaseApp UDP: encrypted login handshake and capture')
    log.info("  Point the client at these; press Ctrl+C to stop.")
    log.info("=" * 62)
    services = [('http', run_http), ('baseapp-udp', run_udp_baseapp),
                ('login-tcp', run_login), ('baseapp-tcp', run_baseapp),
                ('login-udp', run_udp_login)]
    threads = [threading.Thread(target=guarded, args=(name, target),
                               daemon=True, name=name) for name, target in services]
    for t in threads:
        t.start()
    try:
        for name, ready in READY.items():
            if not ready.wait(5):
                raise RuntimeError('Service did not become ready: ' + name)
        log.info('READY: all five listeners are bound; RSA resource key matches')
        while not STOP.is_set() and all(t.is_alive() for t in threads):
            STOP.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down ...")
        STOP.set()
        log.info("evidence: %s , %s" % (login_server.EVID, baseapp_server.EVID))
        log.info("HTTP requests: %s" % http_update_server._LOG)


if __name__ == "__main__":
    from service_guard import service_lease
    with service_lease():
        main()
