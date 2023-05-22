import os
import re
import csv

from datetime import date
from functools import wraps
from bottle import TEMPLATES, debug
from bottleext import get, post, run, request, template, redirect, static_file, url, response, template_user

import psycopg2, psycopg2.extensions, psycopg2.extras
psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)

from auth_public import *
from Podatki import get_history as gh
from graphs import graph_html, graph_cake, graph_stats, analyze

from Database import Repo
from modeli import *
from Services import AuthService

repo = Repo()
auth = AuthService(repo)


debug(True)

# Privzete nastavitve
SERVER_PORT = os.environ.get('BOTTLE_PORT', 8080)
RELOADER = os.environ.get('BOTTLE_RELOADER', True)
DB_PORT = os.environ.get('POSTGRES_PORT', 5432)

# Priklop na bazo
conn = psycopg2.connect(database=db, host=host, user=user, password=password, port=DB_PORT)
cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor) 


user_ime = ''
sporocilo = ''
user_id = 0
user_assets  = []
uspesna_prijava = True  
pravilen_simbol = True
first_load_assets = True
first_load_stats = True
uspesna_registracija = True
anl_stats = (0, 0, 0, 0, 0, 0)
stats_tuple = (0, 0, 0, 0, 0, 0, 0)


@get('/static/<filename:path>')
def static(filename):
   return static_file(filename, root='static')

def cookie_required(f):
    ''' Dekorator, ki zahteva veljaven piškotek. 
        Če piškotka ni, uporabnika preusmeri na stran za prijavo '''
    @wraps(f)
    def decorated( *args, **kwargs): 
        cookie = request.get_cookie('uporabnik')
        if cookie:
            return f(*args, **kwargs)
        
        return template('home.html')
    return decorated


#############################################################
#############      PRIJAVA IN REGISTRACIJA      #############
#############################################################

@get('/')
def zacetna_stran():
    return template('home.html',  pair=cur, naslov='Pomočnik za trgovanje')

@post('/prijava')
def prijava_post():
    global uspesna_prijava, sporocilo, user_id, user_ime
    uporabnisko_ime = request.forms.getunicode('ime')
    geslo = request.forms.getunicode('geslo')

    if not auth.obstaja_uporabnik(uporabnisko_ime):
        return template("home.html", napaka="Uporabnik s tem imenom ne obstaja")

    # Preveri ali sta uporabnisko_ime in geslo pravilna
    prijava = auth.prijavi_uporabnika(uporabnisko_ime, geslo)
    if prijava[0] != 0:
        user_id = prijava[0]
        user_ime = prijava[1]
        sporocilo = ''
        # Nastavi piškotek
        response.set_cookie('uporabnik', uporabnisko_ime)
        redirect('/uporabnik')
    else:
        uspesna_prijava = False
        sporocilo = 'Napačno uporabinško ime ali geslo!'
        return template("prijava.html", napaka="Neuspešna prijava. Napačno geslo ali uporabniško ime.")



@get('/logout')
def logout():
    ''' Odjavi uporabnika in izbriše pikotek '''
    response.delete_cookie('uporabnik')
    redirect('/')

#############################################################

@get('/registracija')
def registracija_get():
    return template('registracija.html', naslov='Registracija')

@post('/registracija')
def registracija_post():
    global uspesna_registracija, sporocilo
    ime = request.forms.name
    priimek = request.forms.surname
    datum_rojstva = request.forms.date_of_birth
    uporabnisko_ime = request.forms.user_name
    geslo = request.forms.password

    # Preveri da uporabnisko_ime še ni zasedeno
    if not auth.obstaja_uporabnik(uporabnisko_ime):
        uspesna_registracija = False
        sporocilo = 'Registracija ni možna, to uporabniško ime že obstaja.'
        redirect('/registracija')
        return template("home.html", napaka=f'Uporabniško ime {uporabnisko_ime} že obstaja')
    else:
        auth.dodaj_uporabnika(ime, priimek, datum_rojstva, uporabnisko_ime, geslo)
        sporocilo = ''
        response.set_cookie('uporabnik', uporabnisko_ime)
        redirect('/uporabnik')
    

#############################################################

@get('/uporabnik')
@cookie_required
def uporabnik():
    global user_id, user_assets
    user_assets = repo.dobi_asset_by_user(asset, user_id)
    
    # V bazi posodobi price_history - če ne dela dodaj: import pandas
    df = gh.update_price_history()
    repo.posodobi_price_history(df)
    return template('uporabnik.html', uporabnik=cur)


#############################################################
##############             NALOŽBE             ##############
#############################################################

@get('/dodaj')
@cookie_required
def dodaj():
    global sporocilo
    sporocilo = ''
    seznam = repo.dobi_pare()
    return template('dodaj_par.html', pair=seznam, naslov='Dodaj naložbo')

@post('/dodaj_potrdi')
def dodaj_potrdi():
    ''' Doda nov par v tabelo pari '''
    global pravilen_simbol, sporocilo
    symbol = request.forms.symbol
    name = request.forms.ime

    # Preveri ali vnešen simbol obstaja
    if gh.preveri_ustreznost('{}'.format(symbol)) == 0:
        pravilen_simbol = False
        sporocilo = 'Vnešen napačen simbol'
        redirect('/dodaj')
    else:
        # Vnese simbol v tabelo par
        repo.dodaj_par(symbol, name)
        gh.get_historic_data(['{}'.format(symbol)], date.today())
        repo.uvozi_Price_History('{}.csv'.format(symbol))
        gh.merge_csv(gh.get_symbols(), 'price_history.csv')
        pravilen_simbol = True
        sporocilo = 'Simbol uspešno dodan'
        redirect('/dodaj')


#############################################################

@get('/assets')
@cookie_required
def assets():
    seznam_asset = repo.dobi_asset_amount_by_user(user_id)
    return template('assets.html', asset=seznam_asset, naslov='Asset')

@post('/buy_sell')
def buy_sell():
    ''' Če kupimo ali prodamo naložbo, ta funkcija
        vnese spremembe v tabelo assets in doda trade '''
    global sporocilo
    symbol = request.forms.symbol
    datum = request.forms.datum
    tip = request.forms.tip
    amount = float(repo.sign(request.forms.amount, tip))

    try:
        # Preveri da smo vnesli pravilen simbol
        repo.dobi_gen_id(pair, symbol, id_col="symbol")
    except:
        sporocilo = 'Napačen simbol!'
        redirect('/assets')

    # Zabeleži trade v tabelo trades
    trejd = trade(  user_id = user_id,
                    symbol_id = symbol, 
                    type = tip,
                    date = datum, 
                    pnl = amount
                )
    repo.dodaj_gen(trejd, serial_col='id_trade')

    # Vnese spremembo v tabelo assets
    repo.trade_result(user_id, symbol, amount)
    sporocilo = 'Transakcija potrjena'
    redirect('/assets')

#############################################################

@get('/performance')
@cookie_required
def performance():
    global first_load_assets, first_load_stats
    # Naloži grafe, če smo program zagnali na novo
    if first_load_assets == True:
       # Pripravi default graf za /performance.html
        graph_html(user_id, user_assets) 
        # Posodobi graf cake.html
        graph_cake(user_id, str(date.today()))
        first_load_assets = False
        first_load_stats = True
    # Počisti cache, da se naloži nov graf 
    TEMPLATES.clear()
    return template('performance.html', assets=user_assets, naslov='Poglej napredek')

@post('/new_equity_graph')
def new_equity_graph():
    simboli_graf = request.forms.simboli
    seznam = re.split(r' ', simboli_graf)
    graph_html(user_id, seznam)
    return redirect('/performance')

@get('/Graphs/assets.html')
def Graf_assets():
    return template('Graphs/assets.html')

@get('/Graphs/cake.html')
def Graf_assets():
    return template('Graphs/cake.html')


#############################################################
#################         TRADES            #################
#############################################################

@get('/trades')
@cookie_required
def trades():
    seznam = repo.dobi_trade_delno(user_id)
    return template('trades.html', trade=seznam, naslov='Dodaj trade')

@post('/dodaj_trade')
def dodaj_trade():
    global sporocilo, user_id
    simbol = request.forms.symbol
    tip = request.forms.type
    strategija = request.forms.strategy
    RR = request.forms.RR
    tarca = request.forms.target
    datum = request.forms.date
    trajanje = request.forms.duration
    TP = request.forms.TP
    PNL = request.forms.PNL

    # Preveri da je simbol veljaven
    row = cur.execute('''SELECT symbol FROM pair WHERE symbol = '{}' '''.format(simbol))
    row = cur.fetchone()
    try:
        # Preveri da smo vnesli pravilen simbol
        repo.dobi_gen_id(pair, simbol, id_col="symbol")
    except:
        sporocilo = 'Napačen simbol, če želite dodati trade za njega, ga najprej dodajte v tabelo pari!'
        redirect('/trades')

    if TP == '':
        TP = psycopg2.extensions.AsIs('NULL')
    trejd = trade(  user_id = user_id,
                    symbol_id = simbol, 
                    type = tip,
                    strategy = strategija,
                    rr = RR,
                    target = tarca,
                    date = datum, 
                    duration = trajanje,
                    tp = TP,
                    pnl = PNL
                )
    # Zabeleži trade v tabelo trades
    repo.dodaj_gen(trejd, serial_col='id_trade')
    # Izid trada poračuna v asset
    repo.pnl_trade(user_id, simbol, PNL)

    sporocilo = 'Trade dodan'
    redirect('/trades')


#############################################################

@get('/stats')
@cookie_required
def stats():
    global stats_tuple, first_load_stats, first_load_assets
    seznam = repo.dobi_strategije(user_id)

    if first_load_stats == True:
        # Pripravi default tuple za /stats.html
        stats_tuple = graph_stats(user_id, 'All')
        first_load_stats = False
        first_load_assets = True
    TEMPLATES.clear()
    return template('stats.html', strategy=seznam, naslov='Statistika')

@post('/strategy')
def strategy():
    global stats_tuple
    strategy = request.forms.strategy

    # V global tuple shrani statistiko  in posodobi graf
    stats_tuple = graph_stats(user_id, strategy)
    TEMPLATES.clear()
    return redirect('/stats')

@get('/Graphs/win_rate.html')
def Graf_assets():
    return template('Graphs/win_rate.html')

@get('/Graphs/win_by_type.html')
def Graf_assets():
    return template('Graphs/win_by_type.html')

@get('/Graphs/pnl_graph.html')
def Graf_assets():
    return template('Graphs/pnl_graph.html')

#############################################################

@get('/analysis')
@cookie_required
def stats():
    seznam = repo.dobi_strategije(user_id)
    return template('analysis.html', strategy=seznam, naslov='Analiza')

@post('/analyze')
def analyze_f():
    global anl_stats
    strat = request.forms.strategy
    duration = int(request.forms.duration)
    rr = int(request.forms.rr)
    target = int(request.forms.target)
    tip = request.forms.tip

    # V global tuple shrani rezultate analize in posodobi graf
    anl_stats = analyze(user_id, strat, duration, rr, target, tip)
    TEMPLATES.clear()
    return redirect('/analysis')


@get('/Graphs/win_rate_anl.html')
def Graf_assets():
    return template('Graphs/win_rate_anl.html')

#############################################################

if __name__ == '__main__':
    run(host='localhost', port=SERVER_PORT, reloader=RELOADER)
