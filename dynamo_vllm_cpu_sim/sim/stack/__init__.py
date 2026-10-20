"""进程化仿真栈（设计文档：docs/多进程真实Dynamo-vLLM仿真栈设计-20261020.md）。

本子包只承载"多进程真实 Dynamo + 真实 vLLM + mock 执行层"形态的新代码；
单进程 DES（sim/run.py 及既有实验）不依赖、不导入本包（决策 D6）。
"""
