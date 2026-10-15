from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class MachineEvent(BaseModel):
    machine_id: str
    status: str 
    stopped_at: str

@app.get("/")
def root ():
    return {"message":"сервис работает"}

@app.post("/api/events")
def recevie_event(event: MachineEvent):
    return{
        "message": "Событие принято",
        "machine_id":event.machine_id,
        "status": event.status,
        "stopped_at":event.stopped_at
    }