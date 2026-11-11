"""Count the entire ASGI request, including streaming and cancellation cleanup."""
from starlette.responses import JSONResponse


class BackupWriteBarrier:
    def __init__(self,app,state):
        self.app,self.state=app,state

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        mutation=scope['method'] not in {'GET','HEAD','OPTIONS'}
        if getattr(self.state,'data_backup',False) and mutation:
            response=JSONResponse({'detail':'Создаётся резервная копия данных. Повторите операцию после завершения.'},status_code=409)
            return await response(scope,receive,send)
        counted=mutation and scope['path']!='/v1/backups/create'
        if counted:self.state.backup_mutations=getattr(self.state,'backup_mutations',0)+1
        try:
            await self.app(scope,receive,send)
        finally:
            if counted:self.state.backup_mutations-=1
