#!/usr/bin/env python

import asyncio
import json
import logging
import uuid
from websockets.asyncio.server import broadcast, serve
import websockets
import random
from itertools import cycle
# from collections import Counter

logging.basicConfig()

from poker_mark1 import compare, calc_waarde

USERS = {}  # Slaat de websocket en bijbehorende UUID op

USERS_LOCK = asyncio.Lock()

class Kaart:
    SUIT_SYMBOLS = {"harten": "♥", "ruiten": "♦", "klaveren": "♣", "schoppen": "♠"}

    def __init__(self, kleur, waarde):
        """
        kleur: str, bijv. "harten", "klaveren"
        waarde: str, bijv. "A", "K", "T"
        """
        self.kleur = kleur
        self.waarde = waarde

class Speler:
    def __init__(self, naam: str, coins: int, hand: list[tuple[Kaart, bool]] = None):
        """
        Parameters:
        - naam: Name of the player.
        - coins: The number of coins the player has.
        - hand: A list of tuples, each containing a Kaart instance and a boolean for face-down status.
        """
        self.naam: str = naam
        self.coins: int = coins
        self.hand: list[tuple[Kaart, bool]] = hand or []
        self.is_AanDeBeurt: bool = False
        self.is_Gepast: bool = False
        self.stoelnummer:int
        self.current_bet:int = 0
        self.mostrecentaction = None

    async def wait_for_action(self):
        await self.action_event.wait()  # Wait for the player to take action
        self.action_event.clear()  # Reset the event for the next round

    def __str__(self):
        return f"{self.naam} ({self.coins} coins), naam = {self.naam}"

class GameState:
    SUIT_SYMBOLS = {"harten": "♥", "ruiten": "♦", "klaveren": "♣", "schoppen": "♠"}
    def __init__(self) -> None:
        self.MAXSPELERS = 8
        self.spelers:dict = {}  # {client_uuid: speler_object}
        self.AanDeBerut:str = None # uuid of player whos turn it is # of stoelnummer?
        self.river = [None, None, None, None, None] # List of cards in river. None represents no card
        self.is_stoel_bezet = [False,False,False,False,False,False,False,False]
        self.pot = 0       # Total coins in the pot
        self.current_bet = 0  # Current highest bet
        self.round_state = ""  # Describes the current phase of the game
        self.highest_bet = 0  # The highest bet in the current round
        self.winners:list[Speler] = []  # List of winners
        self.winners_uuid:list[str] = [] # List of winners uuid


    def create_state_message(self, target_uuid) -> str:
        """
        Genereer een gamestate die alleen informatie bevat die zichtbaar is voor de gevraagde client.
        """
        spelers_data = {}
        for uuid, speler in self.spelers.items():
            winner = None
            if speler in self.winners:
                winner:str = "winner"
            else:
                winner:str = "loser"
            # spelers_data[uuid] = {
            if speler.stoelnummer in spelers_data:
                raise ValueError(f"Duplicate stoelnummer detected: {speler.stoelnummer}")
            
            if (uuid == target_uuid)  or self.round_state == "game_einde":
                hand = [{'kleur':kaart.kleur, "waarde": kaart.waarde}for kaart in speler.hand[:2]]
            else:
                hand = [None, None]

            spelers_data[speler.stoelnummer] = {
                "naam": speler.naam,
                "coins": speler.coins,
                "current_bet": speler.current_bet,
                "mostrecentaction": speler.mostrecentaction,
                "hand": hand,
                # "hand": [
                #     {"kleur": kaart.kleur, "waarde": kaart.waarde} if (uuid == target_uuid or not dicht) and kaart is not None else None
                #     for kaart, dicht in speler.hand
                # ],
                "isAanDeBeurt": speler.is_AanDeBeurt,
                "isGepast": speler.is_Gepast,
                "stoelnummer": speler.stoelnummer,
                "is winner": winner
            }
        return json.dumps({
            "type": "gamestate",
            "spelers": spelers_data,
            "river": [
                {"kleur": kaart.kleur, "waarde": kaart.waarde} if kaart else None
                for kaart in self.river
            ],
            # "aanDeBeurt": self.AanDeBerut,
            "pot": self.pot,
            "highest bid": self.highest_bet,
        })
    

    def handle_client_input(self, event:dict, client_uuid:str)->None:
        # assert that client is allowed to make this action
        # update the gamestate, for example if the action is pass, then set the next player's is_AanDeBeurt to True. 
        logging.info(f"Speler {client_uuid} heeft actie: {event['action']} uitgevoerd.")
        try:
            if event["action"] == "pass":
                self.spelers[client_uuid].mostrecentaction = {"action":'pass'}
            elif event["action"] == "check":
                self.spelers[client_uuid].mostrecentaction = {"action":'check'}
            elif event["action"] == "raise":
                bedrag:int = event.get("amount")
                if bedrag is None: return
                self.spelers[client_uuid].mostrecentaction = {"action":'raise', 'amount': bedrag}
            else:
                raise ValueError("Onbekende actie")
            # Signal that the player has made their move
            self.spelers[client_uuid].action_event.set()
        except Exception as e:
            logging.error(f"Fout bij het verwerken van actie van speler {client_uuid}: {e}")
            pass #idk wat er fout ging maar ik wil niet dat de server crasht. laat maar lekker zitten. Niet mijn probleem.


    def voeg_speler_toe(self, client_uuid, speler):
        if len(self.spelers) >= self.MAXSPELERS:
            raise ValueError("Maximale aantal spelers bereikt.")
        for i,stoel in enumerate(self.is_stoel_bezet):
            if stoel==False:
                speler.stoelnummer = i+1
                self.is_stoel_bezet[i] = True
                break
        speler.is_Gepast = True
        print("[CONNECTION]",f'Beshcikbare stoelen {["X" if stoel else "O" for stoel in self.is_stoel_bezet]}')
        speler.action_event = asyncio.Event()
        self.spelers[client_uuid] = speler

    def verwijder_speler(self, client_uuid):
        try:
            self.spelers[client_uuid].mostrecentaction = "disconnect"
            self.spelers[client_uuid].action_event.set()
            if client_uuid in self.spelers:
                stoel = self.spelers[client_uuid].stoelnummer
                self.is_stoel_bezet[stoel-1] = False
                del self.spelers[client_uuid]
            print("[DISCONNECTION]",f'Beshcikbare stoelen {["X" if stoel else "O" for stoel in self.is_stoel_bezet]}')
        except Exception as e:
            logging.error(f"Fout bij het verwijderen van speler {client_uuid}: {e}")
            pass #idk wat er fout ging maar ik wil niet dat de server crasht. laat maar lekker zitten. Niet mijn probleem.

    def bezette_stoelen(self):
        l = []
        for i,stoel in enumerate(self.is_stoel_bezet):
            if stoel:
                l.append(i+1)
        return l
    
    def actieve_spelers(self)->list[str]:
        l = []
        for uuid,speler in self.spelers.items():
            if not speler.is_Gepast:
                l.append(uuid)
        return l

        
    def deel_kaarten(self):
        for uuid, speler in self.spelers.items():
            speler.hand = [self.kaarten.pop(), self.kaarten.pop()] # :list[Kaart]


    def bet(self,player_uuid:str,amount:int)->None:
        player = self.spelers[player_uuid]
        self.pot+=amount
        player.coins+=-amount
        player.current_bet+=amount
        if player.current_bet > self.highest_bet:
            self.highest_bet = player.current_bet


    def eerste_fase(self,iterator):
        self.current_bet = 0  # Start met een inzet van 0
        self.highest_bet = 0  # De hoogste inzet start op 0
        """Handle the initial blinds phase."""
        self.round_state = "eerste_fase"
        next_player = next(iterator)
        self.bet(next_player,1)
        next_player = next(iterator)
        self.bet(next_player,2)

    async def bied_fase(self, iterator):
        """Verwerkt de biedronde waar elke speler kan passen, checken of raisen."""
        self.round_state = "biedfase"
        print("Biedfase begint")
        if len(self.actieve_spelers()) == 1:
            print("biedfase skipped because of only 1 active player")
            return
        self.current_bet = 0  # Start met een inzet van 0
        # self.highest_bet = 0  # De hoogste inzet start op 0
        actieve_spelers = self.actieve_spelers()  # Alle actieve spelers (niet gepast)
        # iterator = cycle(actieve_spelers)


        self.check_length:int = 0

        # Alle actieve spelers krijgen om beurten de kans om te handelen.
        while True:
            if len(self.actieve_spelers()) == 1:
                break
            # 1 loop van deze loop is 1 beurt van 1 speler

            try:
                speler_uuid = next(iterator)
            except Exception as e:
                print("error",e)
                actieve_spelers = self.actieve_spelers()  # Alle actieve spelers (niet gepast)
                iterator = cycle(actieve_spelers)
                speler_uuid = next(iterator)

            try:
                speler:Speler = self.spelers[speler_uuid]
            except KeyError as e:
                print("error nr 6",e)

                continue
            print(speler.naam, ' is aan de beurt' )
            speler.is_AanDeBeurt = True
            

            if speler.is_Gepast:
                continue  # Sla spelers over die gepast hebben

            print(f"awaiting action from player {speler.naam}")
            await speler.wait_for_action()
            print(f"reveived action from player {speler.naam}")

            # # Wacht op de actie van de speler
            # if speler.mostrecentaction is None:  # Als de speler nog niets heeft gedaan
            #     continue  # Wacht op actie

            if speler.mostrecentaction is "disconnect":
                continue
            actie = speler.mostrecentaction["action"]
            if actie == "pass":
                if speler.current_bet < self.highest_bet:                    
                    speler.is_Gepast = True  # Markeer de speler als gepast
                    logging.info(f"Speler {speler.naam} heeft gepast.")
                    self.check_length:int = 0

                elif speler.current_bet == self.highest_bet:
                    # speler kan niet passen als de inzet gelijk is aan de hoogste inzet
                    # goto actie == "check"
                    speler.is_AanDeBeurt = False
                    self.check_length+=1
                     # do nothing
                elif speler.current_bet > self.highest_bet:
                    # speler kan niet passen als de inzet hoger is dan de hoogste inzet
                    # goto actie == "raise"
                    raise ValueError("Speler kan niet passen als zijn inzet hoger is dan de hoogste inzet")
                    exit() # crash because idk how this happened
            elif actie == "check":
                # Check of de speler de huidige inzet gelijk houdt
                if speler.current_bet < self.highest_bet:
                    # Als de inzet niet gelijk is, moet de speler de juiste hoeveelheid betalen
                    ontbrekend_bedrag = self.highest_bet - speler.current_bet
                    self.bet(speler_uuid, ontbrekend_bedrag)
                    self.check_length+=1
                    logging.info(f"Speler {speler.naam} heeft gecheckt.")
                elif speler.current_bet == self.highest_bet:
                    logging.info(f"Speler {speler.naam} heeft gecheckt.")
                    speler.is_AanDeBeurt = False
                    self.check_length+=1
                    # continue
                elif speler.current_bet > self.highest_bet:
                    # speler kan niet checken als de inzet hoger is dan de hoogste inzet
                    # goto actie == "raise"
                    print(f"speler.current_bet: {speler.current_bet}, self.highest_bet: {self.highest_bet}")
                    raise ValueError("Speler kan niet checken als zijn inzet hoger is dan de hoogste inzet")
                    exit()

            elif actie == "raise":
                # Als de speler raise, verhogen we de inzet
                bedrag = speler.mostrecentaction.get("amount", 0)

                target_bet = speler.current_bet + bedrag

                # moet deze check perse hier
                if target_bet < self.highest_bet:

                    logging.warning(f"Speler {speler.naam} probeert te raisen met een bedrag dat lager is dan de hoogste inzet.")
                    # this action is invalid
                    # simply act as if the player has checked
                    speler.is_Gepast = False  # Markeer de speler als gepast
                    self.check_length+=1
                    self.bet(speler_uuid,self.highest_bet - speler.current_bet)

                elif target_bet == self.highest_bet:
                    self.bet(speler_uuid, bedrag)
                    self.check_length+=1

                elif target_bet > self.highest_bet:
                    self.bet(speler_uuid, bedrag)
                    logging.info(f"Speler {speler.naam} heeft verhoogd naar {bedrag}.")
                    self.check_length:int = 1

            speler.is_AanDeBeurt = False  # Speler is klaar met handelen

            print("[DEBUG] Check length: ",self.check_length)
            print("[DEBUG] Actieve spelers: ",self.actieve_spelers())
            # Controleer of de biedronde klaar is (alle spelers hebben dezelfde inzet of gepast)
            if all(speler.is_Gepast for speler in self.spelers.values()) or self.check_length >= len(self.actieve_spelers()):
                print("einde biedronde(1)")
                break  # Einde biedronde

        self.round_state = "fase_einde"
        logging.info("Biedronde is geëindigd.")
        print("einde biedronde(2).")


    def bepaal_winnaar(self):
        """Bepaal de winnaar van de ronde en deel de pot uit."""
        print("[DEBUG] Bepaal de winnaar van de ronde en deel de pot uit.")
        actieve_spelers = self.actieve_spelers()
        if len(actieve_spelers) == 1:
            print("[DEBUG] 1 speler over, die wint de pot.")
            winnaar_uuid = actieve_spelers[0]
            winnaar = self.spelers[winnaar_uuid]
            winnaar.coins += self.pot
            self.winners = [winnaar]
        else:
            print("[DEBUG] Meerdere spelers over, bepaal de winnaar.")
            n = len(actieve_spelers)
            wins = [0]*n
            for speler in actieve_spelers:
                print("Hand van speler: ",self.spelers[speler].naam)
                self.spelers[speler].hand = self.spelers[speler].hand + self.river
                for kaart in self.spelers[speler].hand:
                    print(f"[DEBUG 563] Kaart: {kaart.kleur} {kaart.waarde}")
            for i in range(n):
                for j in range(i+1,n):
                    hand1:list[Kaart] = self.spelers[actieve_spelers[i]].hand
                    hand2:list[Kaart] = self.spelers[actieve_spelers[j]].hand
                    # this may cause spam
                    print(f"[DEBUG] Comparing hands: {hand1} and {hand2}")
                    result = self.compare(hand1=hand1,hand2=hand2)
                    print(f"[DEBUG] Result: {result}")
                    if result == "1":
                        wins[i]+=1
                    elif result == "2":
                        wins[j]+=1
                    else:
                        wins[i]+=0.5
                        wins[j]+=0.5
            print("[DEBUG] wins: ",wins)
            # winners = Counter(wins)
            # max_win = winners.most_common()[0][0]
            # print(max_win)
            # n_winners = winners.most_common()[0][1]
            max_win = max(wins) # what if there are multiple winners
            if wins.count(max_win) > 1:
                print("[DEBUG] Meerdere winnaars gevonden.")
                # Handle the case where there are multiple winners
                # Split the pot equally among the winners
                n_winners = wins.count(max_win)
                prize = self.pot / n_winners
                for i in range(n):
                    if wins[i] == max_win:
                        winnaar_uuid = actieve_spelers[i]
                        self.spelers[winnaar_uuid].coins += prize
                        self.winners.append(self.spelers[winnaar_uuid])
                        self.winners_uuid.append(winnaar_uuid)
            else:
                winnaar_uuid = actieve_spelers[wins.index(max_win)]
                winnaar = self.spelers[winnaar_uuid]
                winnaar.coins += self.pot
                self.winners = [winnaar]
                self.winners_uuid = [winnaar_uuid]
            print(f"[DEBUG] Speler {self.spelers[winnaar_uuid].naam} wint de pot van {self.pot} coins.")
            # self.winners = []
            # self.winners_uuid = []
            # for i in range(n):
            #     if wins[i] == max_win:
            #         print("Found winner", "uuid:",actieve_spelers[i])
            #         self.winners.append(self.spelers[actieve_spelers[i]])
            #         self.winners_uuid.append(actieve_spelers[i])
            # n_winners = len(self.winners)
            # prize = self.pot / n_winners
            # print(prize, *self.winners)
            # for winner in self.winners_uuid:
            #     print(f"[DEBUG] Deze loop werkt (loc 2)")
            #     print(winner)
            #     self.spelers[winner].coins += prize

            # winnaar_uuid = actieve_spelers[wins.index(max(wins))]
            # not implemented yet, just give the pot to the first player
            # winnaar = self.spelers[winnaar_uuid]
            # print(winnaar.naam)
        # print(f"[DEBUG] Speler {[self.spelers[winner].naam for winner in self.winners]} wint de pot van {self.pot} coins.")
        # logging.info(f"Speler {[self.spelers[winner].naam for winner in self.winners]} wint de pot van {self.pot} coins.")
    
    async def doe_1_ronde(self,deler_uuid):
        print("Nieuwe ronde begint")
        """Execute one full poker round."""

        # SETUP

        self.pot = 0
        for _,speler in self.spelers.items():
            speler.current_bet = 0

        self.highest_bet = 0
    
        # reset kaarten
        await asyncio.sleep(3)
        self.round_state = "nieuwe_ronde"
        self.river:list[Kaart | None] = [None,None,None,None,None] # None represents the lack of a card.
        for uuid, speler in self.spelers.items():
            speler.hand = [None,None]
            speler.is_Gepast = False
            speler.is_AanDeBeurt = False
            speler.current_inzet = 0
        # schud kaarten
        self.kaarten = [Kaart(kleur, waarde) for kleur in self.SUIT_SYMBOLS.keys() for waarde in ["A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "B", "V", "K"]]
        random.shuffle(self.kaarten)
        self.deel_kaarten()

        self.winners = []

        actieve_spelers:list[str] = self.spelers.keys()
        # turn it into an iterator that can loop
        iterator = cycle(actieve_spelers)
        print("[DEBUG] Making the dealer start.")
        while next(iterator) != deler_uuid:
            await asyncio.sleep(1)
            continue

        # BEGIN

        self.eerste_fase(iterator)
        print("[DEBUG] 0 kaarten in river")
        print("[DEBUG] Actieve spelers: ",self.actieve_spelers())
        await self.bied_fase(iterator)
        self.river[0] = self.kaarten.pop()
        self.river[1] = self.kaarten.pop()
        self.river[2] = self.kaarten.pop()
        print("[DEBUG] 3 kaarten in river")
        print("[DEBUG] Actieve spelers: ",self.actieve_spelers())
        await self.bied_fase(iterator)
        self.river[3] = self.kaarten.pop()
        print("[DEBUG] 4 kaarten in river")
        print("[DEBUG] Actieve spelers: ",self.actieve_spelers())
        await self.bied_fase(iterator)
        self.river[4] = self.kaarten.pop()
        print("[DEBUG] 5 kaarten in river")
        print("[DEBUG] Actieve spelers: ",self.actieve_spelers())
        await self.bied_fase(iterator)
        print("[DEBUG] bepaal winnaar")
        self.bepaal_winnaar()

    #     # Check for winner
    #     # made by a friend
    #     # hand out coins
        self.round_state = "game_einde"



state = GameState()
state.compare = compare
state.calc_waarde = calc_waarde

async def game_loop():
    """
    Periodieke taken voor de game, zoals het bijwerken van de staat.
    """
    # await asyncio.sleep(3)

    while len(state.spelers) < 2:
        print("not enough players")
        await asyncio.sleep(3)
    print("Genoeg spelers")
    await asyncio.sleep(10) # wait for players to vote for start
    print("De game begint")


    while True:
        # Start" een nieuwe ronde
        print("[DEBUG] start pause voor nieuwe ronde")
        await asyncio.sleep(10)
        deler_uuid = random.choice(list(state.spelers.keys()))

        print("[GAME] Game loopt. Bezig met state updates...", "Nieuwe ronde begint")
        await state.doe_1_ronde(deler_uuid)


async def startup_handshake(websocket):
    """
    Registreer de client, geef een unieke UUID terug en voeg een speler toe aan de game.
    """
    client_uuid:str = str(uuid.uuid4())  # Genereer unieke UUID
    async with USERS_LOCK:  # Voorkom race conditions
        USERS[client_uuid] = websocket  # Bewaar websocket met UUID

    print(f"[INFO] Client verbonden met UUID: {client_uuid}")

    try:
        msg = await websocket.recv()
        event: dict = json.loads(msg)
        if "name" in event:
            speler_naam = event["name"]
            # Voeg speler toe aan de game (bijv. state.spelers[client_uuid] = speler_naam)
            print(f"[INFO] Speler geregistreerd: {speler_naam} met UUID: {client_uuid}")
    except (asyncio.CancelledError, asyncio.TimeoutError, websockets.ConnectionClosedError) as e:
        print(f"[ERROR] Verbinding verbroken met UUID: {client_uuid}, fout: {e}")
    finally:
        async with USERS_LOCK:
            if client_uuid in USERS:
                del USERS[client_uuid]
        print(f"[INFO] Client met UUID: {client_uuid} is verwijderd")

    speler_start_coins = 100  # Standaard aantal coins
    nieuwe_speler = Speler(naam=speler_naam, coins=speler_start_coins)
    
    try:
        state.voeg_speler_toe(client_uuid, nieuwe_speler)
        print(f"[INFO] {speler_naam} toegevoegd aan het spel.")
        # Stuur de UUID naar de client
        await websocket.send(json.dumps({"type": "register", "uuid": client_uuid}))
    except ValueError as e:
        await websocket.send(json.dumps({"type": "error", "message": str(e)}))
        return  # Stop als er te veel spelers zijn
    return client_uuid

async def handle_message(websocket, client_uuid):
    """
    Verwerkt berichten van een client.
    """
    try:
        async for message in websocket:
            event = json.loads(message)

            # Controleer of de client zijn UUID meestuurt
            if event.get("uuid") != client_uuid:
                await websocket.send(json.dumps({"type": "error", "message": "Ongeldige UUID"}))
                continue

            # controleer of er een type zit in de boodschap. Iedere geldige boodschap bevat "type"
            if "type" not in event:
                await websocket.send(json.dumps({"type": "error", "message": "Ongeldig bericht"}))
                continue
            # Verwerk acties
            elif event["type"] == "action":
                if not state.spelers[client_uuid].is_AanDeBeurt:
                    continue
                try:
                    state.handle_client_input(event, client_uuid)
                except ValueError as e:
                    await websocket.send(json.dumps({"type": "error", "message": str(e)}))

            if event['type'] == 'request gamestate':
                msg = state.create_state_message(client_uuid) # use uuid or websocket to refer to a specific player?
                await websocket.send(msg)
                        # Verwerk een disconnect event

            if event["type"] == "disconnect":
                logging.info(f"[INFO] Client {client_uuid} heeft verbinding verbroken via disconnect-event.")
                async with USERS_LOCK:
                    USERS.pop(client_uuid, None)  # Verwijder websocket uit USERS
                state.verwijder_speler(client_uuid)  # Verwijder speler uit de game state
                await websocket.send(json.dumps({"type": "info", "message": "Je bent succesvol afgemeld."}))
                return  # Beëindig de communicatie met deze clien
            

    finally:
            print(f"[INFO] Client {client_uuid} is verbroken.")
            state.verwijder_speler(client_uuid)
            try:
                # async with USERS_LOCK:
                #     del USERS[client_uuid]
                if client_uuid in state.spelers:
                    del state.spelers[client_uuid]
                logging.info(f"[INFO] Client {client_uuid} is verbroken.")
            except Exception as e:
                print("[ERRPR] geen idee wat er mis ging",e)
                logging.error(f"[ERROR] Fout bij het verwijderen van de client: {e}")

async def network_manager(websocket):
    """
    De main handler per client:
    - Startup handshake
    - Steady state processing
    """
    client_uuid = await startup_handshake(websocket)
    await handle_message(websocket, client_uuid)

async def main():
    game_task = asyncio.create_task(game_loop())  # Start de game loop
    server_task = serve(network_manager,"IPV4 address", 8000)  # WebSocket server

    print("[INFO] Server gestart op ws://192.168.178.110:8000")
    await asyncio.gather(game_task, server_task)  # Voer beide taken parallel uit



if __name__ == "__main__":
    asyncio.run(main())
