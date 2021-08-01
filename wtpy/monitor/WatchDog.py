import threading
import time
import subprocess
import os
import datetime
import json
import copy
import platform
from typing import ChainMap
import psutil

from .EventReceiver import EventReceiver, EventSink
from .WtLogger import WtLogger

from enum import Enum

def isWindows():
    if "windows" in platform.system().lower():
        return True

    return False

class WatcherSink:

    def __init__(self):
        pass

    def on_start(self, appid:str):
        pass

    def on_stop(self, appid:str):
        pass

    def on_output(self, appid:str, tag:str, time:int, message:str):
        pass

    def on_order(self, appid:str, chnl:str, ordInfo:dict):
        pass

    def on_trade(self, appid:str, chnl:str, trdInfo:dict):
        pass
    
    def on_notify(self, appid:str, chnl:str, message:str):
        pass


class ActionType(Enum):
    '''
    操作类型
    枚举变量
    '''
    AT_START    = 0
    AT_STOP     = 1
    AT_RESTART  = 2

class AppState(Enum):
    '''
    app状态
    枚举变量
    '''
    AS_NotExist     = 901
    AS_NotRunning   = 902
    AS_Running      = 903
    AS_Closed       = 904

class AppInfo(EventSink):
    def __init__(self, appConf:dict, sink:WatcherSink = None, logger:WtLogger=None):
        self.__info__ = appConf

        self._cmd_line = None

        self.__logger__ = logger

        self._lock = threading.Lock()
        self._id = appConf["id"]
        self._check_span = appConf["span"]
        self._guard = appConf["guard"]
        self._redirect = False #appConf["redirect"]
        self._schedule = appConf["schedule"]["active"]
        self._weekflag = appConf["schedule"]["weekflag"]

        self._ticks = 0
        self._state = AppState.AS_NotRunning
        self._procid = None
        self._sink = sink

        self._evt_receiver = None

        if not os.path.exists(appConf["folder"]) or not os.path.exists(appConf["path"]):
            self._state == AppState.AS_NotExist

    def applyConf(self, appConf:dict):
        self._lock.acquire()
        self.__info__ = appConf
        self._check_span = appConf["span"]
        self._guard = appConf["guard"]
        self._redirect = False #appConf["redirect"]
        self._schedule = appConf["schedule"]["active"]
        self._weekflag = appConf["schedule"]["weekflag"]
        self._ticks = 0
        self._lock.release()
        self.__logger__.info("应用%s的调度设置已更新" % (self._id))

    def getConf(self):
        self._lock.acquire()
        ret = copy.copy(self.__info__)
        self._lock.release()
        return ret

    @property
    def cmd_line(self) -> str:
        if self._cmd_line is None:
            self._cmd_line = self.__info__["path"] + " " + self.__info__["param"]
        return self._cmd_line

    def is_running(self, pids) -> bool:
        bNeedCheck = (self._procid is None) or (not psutil.pid_exists(self._procid))
        if bNeedCheck:
            for pid in pids:
                try:
                    pInfo = psutil.Process(pid)
                    cmdLine = pInfo.cmdline()
                    if len(cmdLine) == 0:
                        continue
                    # print(cmdLine)
                    cmdLine = ' '.join(cmdLine)
                    if self.cmd_line.upper() == cmdLine.upper():
                        self._procid = pid
                        self.__logger__.info("应用%s挂载成功，进程ID: %d" % (self._id, self._procid))
                        return True
                except:
                    continue
            return False

        return True

    def run(self):
        if self._state == AppState.AS_Running:
            return

        if "mq_url" in self.__info__ and self.__info__["mq_url"] != '':
            # 如果事件接收器为空或者url发生了改变，则需要重新创建
            bNeedCreate = self._evt_receiver is None or self._evt_receiver.url != self.__info__["mq_url"]
            if bNeedCreate:
                if self._evt_receiver is not None:
                    self._evt_receiver.release()
                self._evt_receiver = EventReceiver(self.__info__["mq_url"] != '')
                self.__logger__.info("应用%s开始接收%s的通知信息" % (self._id, self.__info__["mq_url"]))

        try:
            if isWindows():
                self._procid = subprocess.Popen([self.__info__["path"], self.__info__["param"]],  # 需要执行的文件路径
                                cwd=self.__info__["folder"], creationflags=subprocess.CREATE_NEW_CONSOLE).pid
            else:
                self._procid = subprocess.Popen([self.__info__["path"], self.__info__["param"]],  # 需要执行的文件路径
                                cwd=self.__info__["folder"]).pid

            self._cmd_line = self.__info__["path"] + " " + self.__info__["param"]
        except:
            self.__logger__.info("应用%s启动异常" % (self._id))

        self._state = AppState.AS_Running

        self.__logger__.info("应用%s的已启动，进程ID: %d" % (self._id, self._procid))
        if self._sink is not None:
            self._sink.on_start(self._id)

    def stop(self):
        if self._state != AppState.AS_Running:
            return

        try:
            os.kill(self._procid, 0)
        except:
            pass
        self.__logger__.info("应用%s的已停止，进程ID: %d" % (self._id, self._procid))
        if self._sink is not None:
            self._sink.on_stop(self._id)
        self._procid = None
        self._state = AppState.AS_Closed

    def restart(self):
        if self._procid is not None:
            self.stop()
        
        self.run()

    def update_state(self, pids):
        if self.is_running(pids):
            self._state = AppState.AS_Running
        elif self._state == AppState.AS_Running:
            self._state = AppState.AS_NotRunning
            self.__logger__.info("应用%s的已停止" % (self._id))
            self._procid = None
            if self._sink is not None:
                self._sink.on_stop(self._id)
        

    def tick(self, pids):
        self._ticks += 1

        if self._ticks == self._check_span:
            self.update_state(pids)
            if self._state == AppState.AS_NotRunning and self._guard:
                self.__logger__.info("应用%s未启动，正在自动重启" % (self._id))
                self.run()
            elif self._schedule:
                self.__schedule__()

            self._ticks = 0
    
    def __schedule__(self):
        weekflag = self._weekflag

        now = datetime.datetime.now()
        # python中周一是0，周天是6
        # 但是web端沿用了C++里的规则，周日是0，周六是6，所以做一个变换
        wd = now.weekday() + 1
        if wd == 7:
            wd = 0
        if weekflag[wd] != "1":
            return

        appid = self.__info__["id"]

        curMin = int(now.strftime("%H%M"))
        curDt = int(now.strftime("%y%m%d"))
        self._lock.acquire()
        for tInfo in self.__info__["schedule"]["tasks"]:
            if not tInfo["active"]:
                continue
            
            if "lastDate" in tInfo:
                lastDate = tInfo["lastDate"]
            else:
                lastDate = 0

            if "lastTime" in tInfo:
                lastTime = tInfo["lastTime"]
            else:
                lastTime = 0
            targetTm = tInfo["time"]
            action = tInfo["action"]

            if curMin == targetTm and (curMin != lastTime or curDt != lastDate):
                if action == ActionType.AT_START.value:
                    if self._state not in [AppState.AS_NotExist, AppState.AS_Running]:
                        self.__logger__.info("自动启动应用%s" % (appid))
                        self.run()
                elif action == ActionType.AT_STOP.value:
                    if self._state == AppState.AS_Running:
                        self.__logger__.info("自动停止应用%s" % (appid))
                        self.stop()
                elif action == ActionType.AT_RESTART.value:
                    self.__logger__.info("自动重启应用%s" % (appid))
                    self.restart()

                tInfo["lastDate"] = curDt
                tInfo["lastTime"] = curMin
        self._lock.release()

    def isRunning(self):
        return self._state == AppState.AS_Running

    # EventSink.on_order
    def on_order(self, chnl:str, ordInfo:dict):
        if self._sink is not None:
            self._sink.on_order(self._id, chnl, ordInfo)

    # EventSink.on_trade
    def on_trade(self, chnl:str, trdInfo:dict):
        if self._sink is not None:
            self._sink.on_trade(self._id, chnl, trdInfo)
    
    # EventSink.on_notify
    def on_notify(self, chnl:str, message:str):
        if self._sink is not None:
            self._sink.on_notify(self._id, chnl, message)

    # EventSink.on_log
    def on_log(self, tag:str, time:int, message:str):
        if self._sink is not None:
            self._sink.on_output(self._id, tag, time, message)
        pass

class WatchDog:

    def __init__(self, db, sink:WatcherSink = None, logger:WtLogger=None):
        self.__db_conn__ = db
        self.__apps__ = dict()
        self.__app_conf__ = dict()
        self.__stopped__ = False
        self.__worker__ = None
        self.__sink__ = sink
        self.__logger__ = logger

        #加载调度列表
        cur = self.__db_conn__.cursor()
        for row in cur.execute("SELECT * FROM schedules;"):
            appConf = dict()
            appConf["id"] = row[1]
            appConf["path"] = row[2]
            appConf["folder"] = row[3]
            appConf["param"] = row[4]
            appConf["type"] = row[5]
            appConf["span"] = row[6]
            appConf["guard"] = row[7]=='true'
            appConf["redirect"] = row[8]=='true'
            appConf["schedule"] = dict()
            appConf["schedule"]["active"] = row[9]=='true'
            appConf["schedule"]["weekflag"] = row[10]
            appConf["schedule"]["tasks"] = list()
            appConf["schedule"]["tasks"].append(json.loads(row[11]))
            appConf["schedule"]["tasks"].append(json.loads(row[12]))
            appConf["schedule"]["tasks"].append(json.loads(row[13]))
            appConf["schedule"]["tasks"].append(json.loads(row[14]))
            appConf["schedule"]["tasks"].append(json.loads(row[15]))
            appConf["schedule"]["tasks"].append(json.loads(row[16]))
            self.__app_conf__[appConf["id"]] = appConf
            self.__apps__[appConf["id"]] = AppInfo(appConf, sink, self.__logger__)


    def __watch_impl__(self):
        while not self.__stopped__:
            time.sleep(1)
            pids = psutil.pids()
            for appid in self.__apps__:
                appInfo = self.__apps__[appid]

                appInfo.tick(pids)

    def get_apps(self):
        ret = {}
        for appid in self.__app_conf__:
            bRunning = self.__apps__[appid].isRunning()
            conf = copy.copy(self.__app_conf__[appid])
            conf["running"] = bRunning
            ret[appid] = conf
        return ret

    def run(self):
        if self.__worker__ is None:
            self.__worker__ = threading.Thread(target=self.__watch_impl__, name="WatchDog", daemon=True)
            self.__worker__.start()
            self.__logger__.info("自动调度服务已启动")

    def start(self, appid:str):
        if appid not in self.__apps__:
            return

        self.__logger__.info("手动启动%s" % (appid))
        appInfo = self.__apps__[appid]
        appInfo.run()

    def stop(self, appid:str):
        if appid not in self.__apps__:
            return

        self.__logger__.info("手动停止%s" % (appid))
        appInfo = self.__apps__[appid]
        appInfo.stop()

    def has_app(self, appid:str):
        return appid in self.__apps__

    def restart(self, appid:str):
        if appid not in self.__apps__:
            return

        appInfo = self.__apps__[appid]
        appInfo.restart()
    
    def isRunning(self, appid:str):
        if appid not in self.__apps__:
            return False

        appInfo = self.__apps__[appid]
        return appInfo.isRunning()

    def getAppConf(self, appid:str):
        if appid not in self.__apps__:
            return None
        
        appInfo = self.__apps__[appid]
        return appInfo.getConf()

    def delApp(self, appid:str):
        if appid not in self.__apps__:
            return

        self.__apps__.pop(appid)

        cur = self.__db_conn__.cursor()
        cur.execute("DELETE FROM schedules WHERE appid='%s';" % (appid))
        self.__db_conn__.commit()
        self.__logger__.info("应用%s自动调度已删除" % (appid))

    def applyAppConf(self, appConf:dict, isGroup:bool = False):
        appid = appConf["id"]
        self.__app_conf__[appid] = appConf
        isNewApp = False
        if appid not in self.__apps__:
            isNewApp = True
            self.__apps__[appid] = AppInfo(appConf, self.__sink__, self.__logger__)
        else:
            appInst = self.__apps__[appid]
            appInst.applyConf(appConf)

        guard = 'true' if appConf["guard"] else 'false'
        redirect = 'true' if appConf["redirect"] else 'false'
        schedule = 'true' if appConf["schedule"] else 'false'

        stype = 1 if isGroup else 0

        cur = self.__db_conn__.cursor()
        sql = ''
        if isNewApp:
            sql = "INSERT INTO schedules(appid,path,folder,param,type,span,guard,redirect,schedule,weekflag,task1,task2,task3,task4,task5,task6) \
                    VALUES('%s','%s','%s','%s',%d, %d,'%s','%s','%s','%s','%s','%s','%s','%s','%s','%s');" % (
                    appid, appConf["path"], appConf["folder"], appConf["param"], stype, appConf["span"], guard, redirect, schedule, appConf["schedule"]["weekflag"],
                    json.dumps(appConf["schedule"]["tasks"][0]),json.dumps(appConf["schedule"]["tasks"][1]),json.dumps(appConf["schedule"]["tasks"][2]),
                    json.dumps(appConf["schedule"]["tasks"][3]),json.dumps(appConf["schedule"]["tasks"][4]),json.dumps(appConf["schedule"]["tasks"][5]))
        else:
            sql = "UPDATE schedules SET path='%s',folder='%s',param='%s',type=%d,span='%s',guard='%s',redirect='%s',schedule='%s',weekflag='%s',task1='%s',task2='%s',\
                    task3='%s',task4='%s',task5='%s',task6='%s',modifytime=datetime('now','localtime') WHERE appid='%s';" % (
                    appConf["path"], appConf["folder"], appConf["param"], stype, appConf["span"], guard, redirect, schedule, appConf["schedule"]["weekflag"],
                    json.dumps(appConf["schedule"]["tasks"][0]),json.dumps(appConf["schedule"]["tasks"][1]),json.dumps(appConf["schedule"]["tasks"][2]),
                    json.dumps(appConf["schedule"]["tasks"][3]),json.dumps(appConf["schedule"]["tasks"][4]),json.dumps(appConf["schedule"]["tasks"][5]), appid)
        cur.execute(sql)
        self.__db_conn__.commit()