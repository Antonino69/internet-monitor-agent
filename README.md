# Internet Monitor Agent

Agente Python per Linux che controlla periodicamente la connettività Internet, la rete locale e lo stato delle interfacce di rete. Registra misurazioni e incidenti in un database SQLite ed esegue una diagnostica di rete quando rileva un incidente.

## Funzionalità

- Monitoraggio della scheda di rete fisica e del bridge Linux.
- Ping del gateway e di due destinazioni Internet.
- Test DNS e HTTPS.
- Rilevamento di interruzioni e degrado delle latenze.
- Registrazione di campioni, eventi e incidenti in SQLite.
- Diagnostica automatica quando viene aperto un incidente.
- Report da riga di comando e servizio systemd.

## Requisiti

- Linux con Python 3 (il codice usa annotazioni di tipo che richiedono Python 3.10 o successivo).
- systemd per l'installazione come servizio.
- Strumenti di rete: `ping`, `mtr`, `traceroute`, `ethtool`, `dig` e comandi di sistema quali `ip`, `journalctl` e `resolvectl`, disponibili a seconda della distribuzione.
- Non sono richiesti pacchetti Python di terze parti.

## Configurazione iniziale

Il programma contiene **valori preimpostati per una rete specifica**. Prima dell'uso su un altro computer, modificare all'inizio di `internet_monitor.py`:

```python
GATEWAY = "192.168.178.1"
LOGICAL_IFACE = "br0"
PHYSICAL_IFACE = "enp6s0"
```

Destinazioni di test:

- Cloudflare: `1.1.1.1`
- Google DNS: `8.8.8.8`
- DNS: `www.google.com`
- HTTPS: endpoint Google e Cloudflare definiti nello script.

> Le interfacce `br0` ed `enp6s0` devono esistere sul sistema oppure vanno adattate. Gli indirizzi preimpostati sono esempi della configurazione originale, non un rilevamento automatico.

## Installazione su Ubuntu/Debian

Dalla cartella del progetto:

```bash
sudo apt update
sudo apt install -y iputils-ping mtr-tiny traceroute ethtool dnsutils iproute2

sudo mkdir -p /opt/internet-monitor
sudo cp internet_monitor.py /opt/internet-monitor/
sudo chmod 755 /opt/internet-monitor/internet_monitor.py

sudo cp internet-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now internet-monitor
```

Il file di servizio usa `User=root` e scrive i dati in `/var/lib/internet-monitor/`: valutare i privilegi e la protezione dei dati prima di utilizzarlo su sistemi condivisi.

## Controllo in tempo reale

```bash
sudo systemctl status internet-monitor
sudo journalctl -u internet-monitor -f
```

## Riepilogo ultime 24 ore

```bash
sudo python3 /opt/internet-monitor/internet_monitor.py summary --hours 24
```

## Elenco incidenti

```bash
sudo python3 /opt/internet-monitor/internet_monitor.py incidents
```

## Diagnostica per un incidente

Sostituire `3` con l'ID dell'incidente:

```bash
sudo python3 /opt/internet-monitor/internet_monitor.py diagnostic 3
```

## Database e campionamento

Il database SQLite viene creato di norma in:

```text
/var/lib/internet-monitor/monitor.db
```

È possibile modificare il percorso con la variabile d'ambiente `IM_DB`.

- Intervallo nominale fra campioni: **5 secondi**.
- Un errore viene confermato dopo **2 campioni consecutivi**.
- Il ripristino viene confermato dopo **2 campioni consecutivi**.
- I campioni, gli eventi di stato e la diagnostica vengono conservati per **30 giorni** mediante pulizia periodica.
- I record nella tabella degli incidenti non sono cancellati dalla procedura di pulizia automatica.

Le diagnostiche possono contenere indirizzi IP, rotte e informazioni di sistema: **non pubblicare database o log reali**.

## Licenza

Non è stata ancora scelta una licenza. Se si desidera permettere esplicitamente l'uso e la redistribuzione da parte di terzi, aggiungere un file `LICENSE` con la licenza desiderata.
