"""All implemented Machine instances, keyed by code (A4.2). Machines not
listed here are not wired in this session (see the executor manifest)."""
from rova.domain.machines.sm01_request import MACHINE as SM01
from rova.domain.machines.sm02_quotation import MACHINE as SM02
from rova.domain.machines.sm03_order import MACHINE as SM03
from rova.domain.machines.sm04_order_line import MACHINE as SM04
from rova.domain.machines.sm05_delivery_job import MACHINE as SM05
from rova.domain.machines.sm06_vendor import MACHINE as SM06
from rova.domain.machines.sm07_pharmacy import MACHINE as SM07
from rova.domain.machines.sm08_licence import MACHINE as SM08
from rova.domain.machines.sm09_dispute import MACHINE as SM09
from rova.domain.machines.sm10_return import MACHINE as SM10
from rova.domain.machines.sm11_invoice import MACHINE as SM11
from rova.domain.machines.sm12_vendor_offer import MACHINE as SM12
from rova.domain.machines.sm20_eta_estimate import MACHINE as SM20
from rova.domain.machines.sm21_price_list import MACHINE as SM21
from rova.domain.machines.sm22_mode_switch import MACHINE as SM22
from rova.domain.machines.sm23_pms_link import MACHINE as SM23
from rova.domain.machines.sm24_support_thread import MACHINE as SM24

MACHINES = {
    "SM-01": SM01,
    "SM-02": SM02,
    "SM-03": SM03,
    "SM-04": SM04,
    "SM-05": SM05,
    "SM-06": SM06,
    "SM-07": SM07,
    "SM-08": SM08,
    "SM-09": SM09,
    "SM-10": SM10,
    "SM-11": SM11,
    "SM-12": SM12,
    "SM-20": SM20,
    "SM-21": SM21,
    "SM-22": SM22,
    "SM-23": SM23,
    "SM-24": SM24,
}
