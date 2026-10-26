import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.tests.p6_docker_resources import DockerResourceAdapter

class FakeDocker:
	def __init__(self):
		self.calls=[]
		self.state={'net':('net','network','owner'),'pg':('pg','container','owner'),'py':('py','container','owner')}
	def __call__(self,*args,**kwargs):
		self.calls.append(args)
		rid=args[-1]
		if args[0]=='inspect' or args[:2]==('network','inspect'):
			item=self.state.get(rid)
			if item is None: return (1,'not found')
			return (0,'|'.join(item))
		return (0,'removed')


def main():
	results=[]
	def check(name, fn):
		try:
			fn(); print('PASS '+name); results.append(True)
		except Exception as exc:
			print('FAIL '+name+': '+repr(exc)); results.append(False)
	def setup(fake, run):
		a=DockerResourceAdapter(run,'owner',fake)
		n=a.register_created(kind='network',name='network',create_output='net\n')
		a.register_created(kind='container',name='container',create_output='pg\n',depends_on=(n.resource_id,))
		a.register_created(kind='container',name='container',create_output='py\n',depends_on=(n.resource_id,))
		return a
	fake=FakeDocker()
	check('real IDs and reverse dependencies', lambda: setup(fake,'reverse').teardown()[-1].endswith('/net'))
	def owner():
		fake.state['pg']=('pg','container','other')
		try: setup(fake,'owner'); raise AssertionError
		except RuntimeError: fake.state['pg']=('pg','container','owner')
	check('owner mismatch fails closed',owner)
	def partial():
		fake.calls.clear(); a=DockerResourceAdapter('partial','owner',fake); n=a.register_created(kind='network',name='network',create_output='net\n'); assert a.teardown()==(n.resource_id,)
	check('partial creation only cleans registered',partial)
	def empty():
		try: DockerResourceAdapter('empty','owner',fake).register_created(kind='network',name='network',create_output=''); raise AssertionError
		except ValueError: pass
	check('empty create output rejected',empty)
	def mismatch():
		fake.state['pg']=('wrong','container','owner')
		try: DockerResourceAdapter('id','owner',fake).register_created(kind='container',name='container',create_output='pg\n'); raise AssertionError
		except RuntimeError: fake.state['pg']=('pg','container','owner')
	check('ID mismatch fails closed',mismatch)
	def missing():
		fake.state['pg']=None
		try: setup(fake,'missing'); raise AssertionError
		except Exception: fake.state['pg']=('pg','container','owner')
	check('missing resource is not registered',missing)
	def unknown():
		def bad(*args,**kwargs): return (1,'daemon unavailable')
		try: DockerResourceAdapter("unknown","owner",bad).register_created(kind="network",name="network",create_output="net\n"); raise AssertionError
		except RuntimeError: pass
	check('unknown inspect error fails closed',unknown)
	def retry():
		fail=[True]
		def flaky(*args,**kwargs):
			if fail[0] and args[:2]==('rm','-f'): return (1,'remove failed')
			return fake(*args,**kwargs)
		a=DockerResourceAdapter('retry','owner',flaky); n=a.register_created(kind='network',name='network',create_output='net\n'); a.register_created(kind='container',name='container',create_output='pg\n',depends_on=(n.resource_id,))
		try: a.teardown()
		except RuntimeError: pass
		fail[0]=False; a.teardown()
	check('failed teardown retries',retry)
	print(f'PASS total={sum(results)} failed={len(results)-sum(results)}')
	return 0 if all(results) else 1

if __name__=='__main__': raise SystemExit(main())
