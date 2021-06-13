import ipaddress
import logging

import napalm
from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from netfields import InetAddressField

from net.models import Connection
from netbox.api import NetBox
from peering import call_irr_as_set_resolver, parse_irr_as_set
from peering.enums import (
    BGPRelationship,
    BGPState,
    CommunityType,
    DeviceState,
    IPFamily,
    RoutingPolicyType,
)
from peering.fields import ASNField, CommunityField
from peeringdb.functions import get_ixlan_prefixes, get_shared_internet_exchanges
from peeringdb.models import IXLanPrefix, Network, NetworkContact, NetworkIXLan
from utils.models import ChangeLoggedModel, TaggableModel
from utils.validators import AddressFamilyValidator

from .abstracts import AbstractGroup, BGPSession, Template
from .mixins import PolicyMixin


class AutonomousSystem(ChangeLoggedModel, TaggableModel, PolicyMixin):
    asn = ASNField(unique=True, verbose_name="ASN")
    name = models.CharField(max_length=128)
    name_peeringdb_sync = models.BooleanField(default=True)
    contact_name = models.CharField(max_length=50, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True, verbose_name="Contact e-mail")
    comments = models.TextField(blank=True)
    irr_as_set = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="IRR AS-SET"
    )
    irr_as_set_peeringdb_sync = models.BooleanField(default=True)
    ipv6_max_prefixes = models.PositiveIntegerField(
        blank=True, default=0, verbose_name="IPv6 max prefix"
    )
    ipv6_max_prefixes_peeringdb_sync = models.BooleanField(default=True)
    ipv4_max_prefixes = models.PositiveIntegerField(
        blank=True, default=0, verbose_name="IPv4 max prefix"
    )
    ipv4_max_prefixes_peeringdb_sync = models.BooleanField(default=True)
    import_routing_policies = models.ManyToManyField(
        "RoutingPolicy", blank=True, related_name="%(class)s_import_routing_policies"
    )
    export_routing_policies = models.ManyToManyField(
        "RoutingPolicy", blank=True, related_name="%(class)s_export_routing_policies"
    )
    prefixes = models.JSONField(blank=True, null=True, editable=False)
    affiliated = models.BooleanField(default=False)

    logger = logging.getLogger("peering.manager.peering")

    class Meta:
        ordering = ["asn", "affiliated"]
        permissions = [("send_email", "Can send e-mails to AS contact")]

    @property
    def peeringdb_network(self):
        try:
            return Network.objects.get(asn=self.asn)
        except Network.DoesNotExist:
            return None

    @property
    def peeringdb_contacts(self):
        if self.peeringdb_network:
            return NetworkContact.objects.filter(net=self.peeringdb_network)
        return []

    @property
    def can_receive_email(self):
        return "" != self.contact_email or self.peeringdb_contacts

    @staticmethod
    def create_from_peeringdb(asn):
        try:
            network = Network.objects.get(asn=asn)
        except Network.DoesNotExist:
            return None

        autonomous_system, _ = AutonomousSystem.objects.get_or_create(
            asn=network.asn,
            defaults={
                "name": network.name,
                "irr_as_set": network.irr_as_set,
                "ipv6_max_prefixes": network.info_prefixes6,
                "ipv4_max_prefixes": network.info_prefixes4,
            },
        )

        return autonomous_system

    def __str__(self):
        return f"AS{self.asn} - {self.name}"

    def export_policies(self):
        return self.export_routing_policies.all()

    def import_policies(self):
        return self.import_routing_policies.all()

    def get_absolute_url(self):
        return reverse("peering:autonomoussystem_details", args=[self.pk])

    def get_internet_exchange_peering_sessions_list_url(self):
        return reverse(
            "peering:autonomoussystem_internet_exchange_peering_sessions",
            args=[self.pk],
        )

    def get_direct_peering_sessions_list_url(self):
        return reverse(
            "peering:autonomoussystem_direct_peering_sessions", args=[self.pk]
        )

    def get_direct_peering_sessions(self):
        """
        Returns all direct peering sessions with this AS.
        """
        return DirectPeeringSession.objects.filter(autonomous_system=self)

    def get_ixp_peering_sessions(self):
        """
        Returns all IXP peering sessions with this AS.
        """
        return InternetExchangePeeringSession.objects.filter(autonomous_system=self)

    def get_peering_sessions(self):
        """
        Returns all peering sessions with this AS.
        """
        return self.get_direct_peering_sessions().union(self.get_ixp_peering_sessions())

    def get_internet_exchange_points(self, other):
        """
        Returns all IXPs this AS is peering on (with us).
        """
        return InternetExchange.objects.filter(
            pk__in=Connection.objects.filter(
                pk__in=self.get_ixp_peering_sessions().values_list(
                    "ixp_connection", flat=True
                )
            ).values_list("internet_exchange_point", flat=True),
            local_autonomous_system=other,
        )

    def get_shared_internet_exchange_points(self, other):
        """
        Returns all IXPs this AS has with the other one.
        """
        return InternetExchange.objects.filter(
            peeringdb_ixlan__id__in=get_shared_internet_exchanges(
                self, other
            ).values_list("id", flat=True),
            local_autonomous_system=other,
        )

    def get_missing_peering_sessions(self, other, internet_exchange_point=None):
        """
        Returns all missing peering sessions between this AS and the other one on a
        given IXP. As we are relying on PeeringDB to discover sessions there are no
        points in doing so if the IXP is not linked to a PeeringDB record.

        If the IXP is not specified then missing peering sessions will be returned for
        all shared IXPs between this and the other AS.
        """
        if self == other:
            return NetworkIXLan.objects.none()

        filter = {"autonomous_system": self}
        if internet_exchange_point:
            filter["ixp_connection__id__in"] = Connection.objects.filter(
                internet_exchange_point=internet_exchange_point
            ).values_list("id", flat=True)
        ip_sessions = InternetExchangePeeringSession.objects.filter(
            **filter
        ).values_list("ip_address", flat=True)

        qs_filter = Q(asn=self.asn) & (
            ~Q(ipaddr6__in=ip_sessions) | ~Q(ipaddr4__in=ip_sessions)
        )
        if internet_exchange_point:
            qs_filter &= Q(ixlan=internet_exchange_point.peeringdb_ixlan)
        else:
            qs_filter &= Q(
                ixlan__in=self.get_shared_internet_exchange_points(other).values_list(
                    "peeringdb_ixlan", flat=True
                )
            )
        return NetworkIXLan.objects.filter(qs_filter)

    def synchronize_with_peeringdb(self):
        """
        Synchronizes AS properties with those found in PeeringDB.
        """
        network = self.peeringdb_network
        if not network:
            return False

        if self.name_peeringdb_sync:
            self.name = network.name
        if self.irr_as_set_peeringdb_sync:
            self.irr_as_set = network.irr_as_set
        if self.ipv6_max_prefixes_peeringdb_sync:
            self.ipv6_max_prefixes = network.info_prefixes6
        if self.ipv4_max_prefixes_peeringdb_sync:
            self.ipv4_max_prefixes = network.info_prefixes4

        try:
            self.save()
            return True
        except Exception:
            return False

    def retrieve_irr_as_set_prefixes(self):
        """
        Returns a prefix list for this AS' IRR AS-SET. If none is provided the
        function will try to look for a prefix list based on the AS number.

        This function will actually retrieve prefixes from IRR online sources. It is
        expected to be slow due to network operations and depending on the size of the
        data to process.
        """
        fallback = False
        as_sets = parse_irr_as_set(self.asn, self.irr_as_set)
        prefixes = {"ipv6": [], "ipv4": []}

        try:
            # For each AS-SET try getting IPv6 and IPv4 prefixes
            for as_set in as_sets:
                prefixes["ipv6"].extend(
                    call_irr_as_set_resolver(as_set, address_family=6)
                )
                prefixes["ipv4"].extend(
                    call_irr_as_set_resolver(as_set, address_family=4)
                )
        except ValueError:
            # Error parsing AS-SETs
            fallback = True

        # If fallback is triggered or no prefixes found, try prefix lookup by ASN
        if fallback or not prefixes["ipv6"] and not prefixes["ipv4"]:
            self.logger.debug(
                f"falling back to AS number lookup to search for {self.asn} prefixes"
            )
            prefixes["ipv6"].extend(
                call_irr_as_set_resolver(f"AS{self.asn}", address_family=6)
            )
            prefixes["ipv4"].extend(
                call_irr_as_set_resolver(f"AS{self.asn}", address_family=4)
            )

        return prefixes

    def get_irr_as_set_prefixes(self, address_family=0):
        """
        Returns a prefix list for this AS' IRR AS-SET. If none is provided the list
        will be empty.

        If specified, only a list of the prefixes for the given address family will be
        returned. 6 for IPv6, 4 for IPv4, both for all other values.

        The stored database value will be used if it exists.
        """
        prefixes = (
            self.prefixes if self.prefixes else self.retrieve_irr_as_set_prefixes()
        )

        if address_family == 6:
            return prefixes["ipv6"]
        elif address_family == 4:
            return prefixes["ipv4"]
        else:
            return prefixes

    def get_contact_email_addresses(self):
        """
        Returns a list of all contacts with their respective e-mails addresses.
        The returned list can be used in form choice fields.
        """
        addresses = []

        # Append the contact set by the user if one has been set
        if self.contact_email:
            addresses.append(
                (
                    self.contact_email,
                    f"{self.contact_name} - {self.contact_email}"
                    if self.contact_name
                    else self.contact_email,
                )
            )

        # Append the contacts found in PeeringDB, avoid re-adding a contact if the
        # email address is the same as the one set by the user manually
        for contact in self.peeringdb_contacts:
            if contact.email and contact.email not in [a[0] for a in addresses]:
                addresses.append(
                    (
                        contact.email,
                        f"{contact.name} - {contact.email}"
                        if contact.name
                        else contact.email,
                    )
                )

        return addresses

    def get_email_context(self):
        """
        Returns a dict, to be used in a Jinja2 environment, that holds enough data to
        help in creating an e-mail from a template.
        """
        affiliated = AutonomousSystem.objects.filter(affiliated=True)
        return {"affiliated_autonomous_systems": affiliated, "autonomous_system": self}

    def generate_email(self, email):
        """
        Renders an e-mail from a template.
        """
        return email.render(self.get_email_context())


class BGPGroup(AbstractGroup):
    logger = logging.getLogger("peering.manager.peering")

    class Meta(AbstractGroup.Meta):
        ordering = ["name", "slug"]
        verbose_name = "BGP group"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("peering:bgpgroup_details", args=[self.pk])

    def get_peering_sessions_list_url(self):
        return reverse("peering:bgpgroup_peering_sessions", args=[self.pk])

    def get_peering_sessions(self):
        return DirectPeeringSession.objects.filter(bgp_group=self)

    def poll_peering_sessions(self):
        if not self.check_bgp_session_states:
            self.logger.debug(
                'ignoring session states for %s, reason: "check disabled"',
                self.name.lower(),
            )
            return False

        peering_sessions = DirectPeeringSession.objects.prefetch_related(
            "router"
        ).filter(bgp_group=self)
        if not peering_sessions:
            # Empty result no need to go further
            return False

        # Get BGP neighbors details from router, but only get them once
        bgp_neighbors_detail = {}
        for session in peering_sessions:
            if session.router not in bgp_neighbors_detail:
                detail = session.router.get_bgp_neighbors_detail()
                bgp_neighbors_detail.update(
                    {
                        session.router: session.router.bgp_neighbors_detail_as_list(
                            detail
                        )
                    }
                )

        if not bgp_neighbors_detail:
            # Empty result no need to go further
            return False

        with transaction.atomic():
            for router, detail in bgp_neighbors_detail.items():
                for session in detail:
                    ip_address = session["remote_address"]
                    self.logger.debug(
                        "looking for session %s in %s", ip_address, self.name.lower()
                    )

                    try:
                        peering_session = DirectPeeringSession.objects.get(
                            ip_address=ip_address, bgp_group=self, router=router
                        )

                        # Get info that we are actually looking for
                        state = session["connection_state"].lower()
                        received = session["received_prefix_count"]
                        advertised = session["advertised_prefix_count"]
                        self.logger.debug(
                            "found session %s in %s with state %s",
                            ip_address,
                            self.name.lower(),
                            state,
                        )

                        # Update fields
                        peering_session.bgp_state = state
                        peering_session.received_prefix_count = (
                            received if received > 0 else 0
                        )
                        peering_session.advertised_prefix_count = (
                            advertised if advertised > 0 else 0
                        )
                        # Update the BGP state of the session
                        if peering_session.bgp_state == BGPState.ESTABLISHED:
                            peering_session.last_established_state = timezone.now()
                        peering_session.save()
                    except DirectPeeringSession.DoesNotExist:
                        self.logger.debug(
                            "session %s in %s not found", ip_address, self.name.lower()
                        )

            # Save last session states update
            self.bgp_session_states_update = timezone.now()
            self.save()

        return True


class Community(ChangeLoggedModel, TaggableModel):
    name = models.CharField(max_length=128)
    slug = models.SlugField(unique=True, max_length=255)
    value = CommunityField(max_length=50)
    type = models.CharField(
        max_length=50, choices=CommunityType.choices, default=CommunityType.INGRESS
    )
    comments = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "communities"
        ordering = ["value", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("peering:community_details", args=[self.pk])

    def get_type_html(self):
        if self.type == CommunityType.EGRESS:
            badge_type = "badge-primary"
            text = self.get_type_display()
        elif self.type == CommunityType.INGRESS:
            badge_type = "badge-info"
            text = self.get_type_display()
        else:
            badge_type = "badge-secondary"
            text = "Unknown"

        return mark_safe(f'<span class="badge {badge_type}">{text}</span>')


class DirectPeeringSession(BGPSession):
    local_autonomous_system = models.ForeignKey(
        "AutonomousSystem",
        on_delete=models.CASCADE,
        related_name="%(class)s_local_autonomous_system",
        null=True,
    )
    local_ip_address = InetAddressField(
        store_prefix_length=False,
        blank=True,
        null=True,
        verbose_name="Local IP address",
    )
    bgp_group = models.ForeignKey(
        "BGPGroup",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        verbose_name="BGP group",
    )
    relationship = models.CharField(
        max_length=50,
        choices=BGPRelationship.choices,
        help_text="Relationship with the remote peer.",
    )
    router = models.ForeignKey(
        "Router", blank=True, null=True, on_delete=models.SET_NULL
    )

    class Meta(BGPSession.Meta):
        ordering = [
            "service_reference",
            "local_autonomous_system",
            "autonomous_system",
            "ip_address",
        ]

    def __str__(self):
        return f"{self.get_relationship_display()} - AS{self.autonomous_system.asn} - IP {self.ip_address}"

    def get_absolute_url(self):
        return reverse("peering:directpeeringsession_details", args=[self.pk])

    def poll(self):
        # Check if we are able to get BGP details
        log = 'ignoring session states on {}, reason: "{}"'
        if not self.router or not self.router.platform:
            log = log.format(str(self.ip_address).lower(), "no usable router attached")
        elif self.bgp_group and not self.bgp_group.check_bgp_session_states:
            log = log.format(self.name.lower(), "check disabled")
        else:
            log = None

        # If we cannot check for BGP details, don't do anything
        if log:
            self.logger.debug(log)
            return False

        # Get BGP session detail
        bgp_neighbor_detail = self.router.get_bgp_neighbors_detail(
            ip_address=self.ip_address
        )
        if bgp_neighbor_detail:
            received = bgp_neighbor_detail["received_prefix_count"]
            advertised = bgp_neighbor_detail["advertised_prefix_count"]

            # Update fields
            self.bgp_state = bgp_neighbor_detail["connection_state"].lower()
            self.received_prefix_count = received if received > 0 else 0
            self.advertised_prefix_count = advertised if advertised > 0 else 0
            if self.bgp_state == BGPState.ESTABLISHED:
                self.last_established_state = timezone.now()
            self.save()
            return True

        return False

    def get_relationship_html(self):
        if self.relationship == BGPRelationship.CUSTOMER:
            badge_type = "badge-danger"
        elif self.relationship == BGPRelationship.PRIVATE_PEERING:
            badge_type = "badge-success"
        elif self.relationship == BGPRelationship.TRANSIT_PROVIDER:
            badge_type = "badge-primary"
        else:
            badge_type = "badge-secondary"

        return mark_safe(
            f'<span class="badge {badge_type}">{self.get_relationship_display()}</span>'
        )


class InternetExchange(AbstractGroup):
    peeringdb_ixlan = models.ForeignKey(
        "peeringdb.IXLan", on_delete=models.SET_NULL, blank=True, null=True
    )
    local_autonomous_system = models.ForeignKey(
        "AutonomousSystem", on_delete=models.CASCADE, null=True
    )

    logger = logging.getLogger("peering.manager.peering")

    class Meta(AbstractGroup.Meta):
        ordering = ["local_autonomous_system", "name", "slug"]

    @property
    def linked_to_peeringdb(self):
        """
        Tells if the PeeringDB object for this IX still exists.
        """
        return self.peeringdb_ixlan is not None

    @property
    def has_connected_routers(self):
        return (
            Connection.objects.filter(
                internet_exchange_point=self, router__isnull=False
            ).count()
            > 0
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("peering:internetexchange_details", args=[self.pk])

    def get_peering_sessions_list_url(self):
        return reverse("peering:internetexchange_peering_sessions", args=[self.pk])

    def get_peer_list_url(self):
        return reverse("peering:internet_exchange_peers", args=[self.pk])

    def merged_export_policies(self, reverse=False):
        # Get own policies
        policies = [p for p in self.export_policies()]

        return list(reversed(policies)) if reverse else policies

    def merged_import_policies(self, reverse=False):
        # Get own policies
        policies = [p for p in self.import_policies()]

        return list(reversed(policies)) if reverse else policies

    def link_to_peeringdb(self):
        """
        Retrieves the PeeringDB IDs for this IX based on connections.

        The PeeringDB records will be returned on success. In any other cases `None`
        will be returned. The value will also be saved in model's field.
        """
        peeringdb_ixlan = None
        for connection in Connection.objects.filter(internet_exchange_point=self):
            # For each connection, try to see if a valid PeeringDB record exists and
            # make sure that they all point towards the same IX
            if connection.linked_to_peeringdb:
                if peeringdb_ixlan is None:
                    peeringdb_ixlan = connection.peeringdb_netixlan.ixlan
                else:
                    if peeringdb_ixlan != connection.peeringdb_netixlan.ixlan:
                        # Connections not belonging to the same IX
                        return None

        if peeringdb_ixlan is not None:
            self.peeringdb_ixlan = peeringdb_ixlan
            self.save()

        return peeringdb_ixlan

    def get_prefixes(self):
        """
        Returns all prefixes found (in PeeringDB) for this IXP.
        """
        if not self.linked_to_peeringdb:
            return IXLanPrefix.objects.none()

        return IXLanPrefix.objects.filter(ixlan=self.peeringdb_ixlan)

    def get_connections(self):
        """
        Returns all connections to this IXP.
        """
        return Connection.objects.filter(internet_exchange_point=self)

    def get_routers(self):
        return Router.objects.filter(
            pk__in=self.get_connections().values_list("router", flat=True)
        )

    def get_peering_sessions(self):
        """
        Returns all peering sessions setup over this IXP.
        """
        return InternetExchangePeeringSession.objects.filter(
            ixp_connection__in=self.get_connections()
        )

    def get_autonomous_systems(self):
        """
        Returns all autonomous systems with setup peering sessions over this IXP.
        """
        return AutonomousSystem.objects.filter(
            pk__in=self.get_peering_sessions().values_list(
                "autonomous_system", flat=True
            )
        )

    def get_available_peers(self):
        """
        Finds available peers for the AS connected to this IX.
        """
        # Not linked to PeeringDB, cannot determine peers
        if not self.linked_to_peeringdb:
            return NetworkIXLan.objects.none()

        # Get all peering sessions currently existing
        existing_sessions = self.get_peering_sessions()
        ipv6_sessions = []
        ipv4_sessions = []
        for session in existing_sessions:
            ip = ipaddress.ip_address(session.ip_address)
            if ip.version == 6:
                ipv6_sessions.append(ip)
            elif ip.version == 4:
                ipv4_sessions.append(ip)
            else:
                self.logger.debug(f"peering session with strange ip: {ip}")

        return NetworkIXLan.objects.filter(
            ~Q(asn=self.local_autonomous_system.asn)
            & Q(ixlan=self.peeringdb_ixlan)
            & (~Q(ipaddr6__in=ipv6_sessions) | ~Q(ipaddr4__in=ipv4_sessions))
        ).order_by("asn")

    @transaction.atomic
    def poll_peering_sessions(self):
        # Get connected routers to this IXP
        connected_routers = self.get_routers()

        # Check if we are able to get BGP details
        log = 'ignoring session states on {}, reason: "{}"'
        if connected_routers.count() < 0:
            log = log.format(self.name.lower(), "no routers connected")
        elif not self.check_bgp_session_states:
            log = log.format(self.name.lower(), "check disabled")
        else:
            log = None

        # If we cannot check for BGP details, don't do anything
        if log:
            self.logger.debug(log)
            return False

        for router in connected_routers:
            # Get all BGP sessions detail
            bgp_neighbors_detail = router.get_bgp_neighbors_detail()

            # An error occured, probably
            if not bgp_neighbors_detail:
                return False

            for _, as_details in bgp_neighbors_detail.items():
                for _, sessions in as_details.items():
                    # Check BGP sessions found
                    for session in sessions:
                        ip_address = session["remote_address"]
                        self.logger.debug(
                            f"looking for session {ip_address} in {self.name.lower()}"
                        )

                        # Check if the BGP session is on this IX
                        try:
                            ixp_session = InternetExchangePeeringSession.objects.get(
                                ip_address=ip_address
                            )
                            # Get the BGP state for the session
                            state = session["connection_state"].lower()
                            received = session["received_prefix_count"]
                            advertised = session["advertised_prefix_count"]
                            self.logger.debug(
                                f"found session {ip_address} in {self.name.lower()} with state {state}"
                            )

                            # Update fields
                            ixp_session.bgp_state = state
                            ixp_session.received_prefix_count = received or 0
                            ixp_session.advertised_prefix_count = advertised or 0
                            # Update the BGP state of the session
                            if ixp_session.bgp_state == BGPState.ESTABLISHED:
                                ixp_session.last_established_state = timezone.now()
                            ixp_session.save()
                        except InternetExchangePeeringSession.DoesNotExist:
                            self.logger.debug(
                                f"session {ip_address} in {self.name.lower()} not found"
                            )

                # Save last session states update
                self.bgp_session_states_update = timezone.now()
                self.save()

        return True

    @transaction.atomic
    def import_sessions(self, connection):
        """
        Imports sessions setup on a connected router.
        """
        session_number, asn_number = 0, 0
        ignored_autonomous_systems = []

        allowed_prefixes = self.get_prefixes()
        sessions = connection.router.get_bgp_neighbors()

        def is_valid(ip_address):
            for p in allowed_prefixes:
                if p.prefix.version == ip_address.version:
                    if ip_address in p.prefix:
                        return True
            return False

        for session in sessions:
            ip = ipaddress.ip_address(session["ip_address"])
            if not is_valid(ip):
                self.logger.debug(
                    f"ignoring ixp session, {str(ip)} does not fit in any prefixes"
                )
                continue

            self.logger.debug(f"processing ixp session {str(ip)}")
            remote_asn = session["remote_asn"]

            try:
                InternetExchangePeeringSession.objects.get(
                    ixp_connection=connection, ip_address=ip
                )
                self.logger.debug(
                    f"ixp session {str(ip)} with as{remote_asn} already exists"
                )
                continue
            except InternetExchangePeeringSession.DoesNotExist:
                self.logger.debug(
                    f"ixp session {str(ip)} with as{remote_asn} does not exist"
                )

            # Get the AS, create it if needed
            autonomous_system = AutonomousSystem.create_from_peeringdb(remote_asn)

            # Do not count the AS if it does not have a PeeringDB record
            if autonomous_system:
                self.logger.debug(f"as{remote_asn} created")
                asn_number += 1
            else:
                if remote_asn not in ignored_autonomous_systems:
                    ignored_autonomous_systems.append(remote_asn)
                    self.logger.debug(
                        f"could not create as{remote_asn}, session {str(ip)} ignored"
                    )

            # Only add a session if we can use the AS it is linked to
            if autonomous_system:
                self.logger.debug(f"creating session {str(ip)}")
                InternetExchangePeeringSession.objects.create(
                    autonomous_system=autonomous_system,
                    ixp_connection=connection,
                    ip_address=ip,
                )
                session_number += 1
                self.logger.debug(f"session {str(ip)} created")

        return session_number, asn_number


class InternetExchangePeeringSession(BGPSession):
    ixp_connection = models.ForeignKey(
        "net.Connection",
        on_delete=models.CASCADE,
        null=True,
        verbose_name="IXP connection",
    )
    is_route_server = models.BooleanField(
        blank=True, default=False, verbose_name="Route server"
    )

    class Meta(BGPSession.Meta):
        ordering = [
            "service_reference",
            "autonomous_system",
            "ixp_connection",
            "ip_address",
        ]

    @staticmethod
    def create_from_peeringdb(affiliated, netixlan):
        results = []

        if not netixlan:
            return results

        available_ixp = InternetExchange.objects.get(peeringdb_ixlan=netixlan.ixlan)
        available_connections = Connection.objects.filter(
            internet_exchange_point=available_ixp
        )

        for connection in available_connections:
            for version in [6, 4]:
                ip_address = getattr(netixlan, f"ipaddr{version}", None)
                if not ip_address:
                    continue

                params = {
                    "autonomous_system": AutonomousSystem.create_from_peeringdb(
                        netixlan.asn
                    ),
                    "ixp_connection": connection,
                    "ip_address": ip_address.ip,
                }

                try:
                    # Try to get the session, in case it already exists
                    InternetExchangePeeringSession.objects.get(**params)
                except InternetExchangePeeringSession.DoesNotExist:
                    results.append(InternetExchangePeeringSession(**params))

        return results

    def __str__(self):
        if not self.ixp_connection:
            return f"AS{self.autonomous_system.asn} - IP {self.ip_address}"
        return f"{self.ixp_connection.internet_exchange_point.name} - AS{self.autonomous_system.asn} - IP {self.ip_address}"

    def get_absolute_url(self):
        return reverse("peering:internetexchangepeeringsession_details", args=[self.pk])

    def poll(self):
        # Check if we are able to get BGP details
        log = 'ignoring session states on {}, reason: "{}"'
        if not self.ixp_connection.router or not self.ixp_connection.router.platform:
            log = log.format(str(self.ip_address).lower(), "no usable router attached")
        else:
            log = None

        # If we cannot check for BGP details, don't do anything
        if log:
            self.logger.debug(log)
            return False

        # Get BGP session detail
        bgp_neighbor_detail = self.ixp_connection.router.get_bgp_neighbors_detail(
            ip_address=self.ip_address
        )
        if bgp_neighbor_detail:
            received = bgp_neighbor_detail["received_prefix_count"]
            advertised = bgp_neighbor_detail["advertised_prefix_count"]

            # Update fields
            self.bgp_state = bgp_neighbor_detail["connection_state"].lower()
            self.received_prefix_count = received if received > 0 else 0
            self.advertised_prefix_count = advertised if advertised > 0 else 0
            if self.bgp_state == BGPState.ESTABLISHED:
                self.last_established_state = timezone.now()
            self.save()
            return True

        return False

    def exists_in_peeringdb(self):
        """
        Returns `True` if a NetworkIXLan exists for this session's IP.
        """
        if isinstance(self.ip_address, str):
            ip_version = ipaddress.ip_address(self.ip_address).version
        else:
            ip_version = self.ip_address.version
        try:
            NetworkIXLan.objects.get(**{f"ipaddr{ip_version}": str(self.ip_address)})
            return True
        except NetworkIXLan.DoesNotExist:
            pass
        return False

    def is_abandoned(self):
        """
        Returns True if a session is considered as abandoned. Returns False otherwise.

        A session is *not* considered as abandoned if it matches one of the following
        criteria:
          * The Internet Exchange is not linked to a PeeringDB record
          * User does not poll peering session states
          * The peer AS has no cached PeeringDB record
          * The peer AS has a cached PeeringDB record with the session IP address
          * The BGP state for the session is not idle or active
        """
        if (
            not self.ixp_connection.linked_to_peeringdb
            or not self.ixp_connection.internet_exchange_point.check_bgp_session_states
            or not self.autonomous_system.peeringdb_network
            or self.exists_in_peeringdb()
            or self.bgp_state not in [BGPState.IDLE, BGPState.ACTIVE]
        ):
            return False
        return True


class Router(ChangeLoggedModel, TaggableModel):
    local_autonomous_system = models.ForeignKey(
        "AutonomousSystem", on_delete=models.CASCADE, null=True
    )
    name = models.CharField(max_length=128)
    hostname = models.CharField(max_length=256)
    platform = models.ForeignKey(
        "devices.Platform",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        help_text="The router platform, used to interact with it",
    )
    encrypt_passwords = models.BooleanField(
        blank=True,
        default=False,
        help_text="Try to encrypt passwords for peering sessions",
    )
    configuration_template = models.ForeignKey(
        "Configuration", blank=True, null=True, on_delete=models.SET_NULL
    )
    device_state = models.CharField(
        max_length=20,
        choices=DeviceState.choices,
        blank=True,
        help_text="State of the device for configuration pushes",
    )
    netbox_device_id = models.PositiveIntegerField(
        blank=True, default=0, verbose_name="NetBox device"
    )
    use_netbox = models.BooleanField(
        blank=True,
        default=False,
        help_text="Use NetBox to communicate instead of NAPALM",
    )
    config_context = models.JSONField(blank=True, null=True)
    napalm_username = models.CharField(blank=True, null=True, max_length=256)
    napalm_password = models.CharField(blank=True, null=True, max_length=256)
    napalm_timeout = models.PositiveIntegerField(blank=True, default=0)
    napalm_args = models.JSONField(blank=True, null=True)
    comments = models.TextField(blank=True)

    logger = logging.getLogger("peering.manager.napalm")

    class Meta:
        ordering = ["local_autonomous_system", "name"]
        permissions = [
            ("view_router_configuration", "Can view router's configuration"),
            ("deploy_router_configuration", "Can deploy router's configuration"),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("peering:router_details", args=[self.pk])

    def get_direct_peering_sessions_list_url(self):
        return reverse("peering:router_direct_peering_sessions", args=[self.pk])

    def is_netbox_device(self):
        return self.netbox_device_id != 0

    def is_usable_for_task(self, job_result=None, logger=None):
        """
        Performs pre-flight checks to understand if a router is suited for background
        task processing.
        """
        if logger is None:
            logger = self.logger

        # Ensure device is not in disabled state
        if self.device_state == DeviceState.DISABLED:
            if job_result:
                job_result.mark_errored(
                    "Router is not enabled.", obj=self, logger=logger
                )
                job_result.save()
            return False

        # Check if the router runs on a supported platform
        if not self.platform:
            if job_result:
                job_result.mark_errored(
                    "Router has no assigned platform.", obj=self, logger=logger
                )
                job_result.save()
            return False
        if not self.platform.napalm_driver:
            if job_result:
                job_result.mark_errored(
                    "Router's platform has no NAPALM driver.", obj=self, logger=logger
                )
                job_result.save()
            return False

        return True

    def get_bgp_groups(self):
        """
        Returns BGP groups that can be deployed on this router.

        A group is considered as deployable on a router if direct peering sessions in
        the group are also attached to the router.
        """
        return BGPGroup.objects.filter(
            pk__in=DirectPeeringSession.objects.filter(router=self).values_list(
                "bgp_group", flat=True
            )
        )

    def get_connections(self, internet_exchange_point=None):
        """
        Returns connections attached to this router.
        """
        if internet_exchange_point:
            return Connection.objects.filter(
                internet_exchange_point=internet_exchange_point, router=self
            )
        else:
            return Connection.objects.filter(router=self)

    def get_internet_exchange_points(self):
        """
        Returns IXPs that this router is connected to.
        """
        return InternetExchange.objects.filter(
            pk__in=self.get_connections().values_list(
                "internet_exchange_point", flat=True
            )
        )

    def get_direct_autonomous_systems(self, bgp_group=None):
        """
        Returns autonomous systems that are directly peered with this router.
        """
        if bgp_group:
            sessions = DirectPeeringSession.objects.filter(
                bgp_group=bgp_group, router=self
            ).values_list("autonomous_system", flat=True)
        else:
            sessions = DirectPeeringSession.objects.filter(router=self).values_list(
                "autonomous_system", flat=True
            )
        return AutonomousSystem.objects.filter(pk__in=sessions)

    def get_ixp_autonomous_systems(self, internet_exchange_point=None):
        """
        Returns autonomous systems with which this router peers over IXPs.
        """
        return AutonomousSystem.objects.filter(
            pk__in=InternetExchangePeeringSession.objects.filter(
                ixp_connection__in=self.get_connections(
                    internet_exchange_point=internet_exchange_point
                )
            ).values_list("autonomous_system", flat=True)
        )

    def get_autonomous_systems(self):
        """
        Returns all autonomous systems with which this router peers.
        """
        return self.get_direct_autonomous_systems().union(
            self.get_ixp_autonomous_systems()
        )

    def get_configuration_context(self):
        """
        Returns a dict, to be used in a Jinja2 environment, that holds enough data to
        help in creating a configuration from a template.
        """
        return {
            "autonomous_systems": self.get_autonomous_systems(),
            "bgp_groups": self.get_bgp_groups(),
            "communities": Community.objects.all(),
            "internet_exchange_points": self.get_internet_exchange_points(),
            "local_as": self.local_autonomous_system,
            "routing_policies": RoutingPolicy.objects.all(),
            "router": self,
        }

    def generate_configuration(self):
        """
        Returns the configuration of a router according to the template in use.

        If no template is used, an empty string is returned.
        """
        if self.configuration_template:
            context = self.get_configuration_context()
            return self.configuration_template.render(context)
        else:
            return ""

    def get_napalm_device(self):
        """
        Returns an instance of the NAPALM driver to connect to a router.
        """
        if not self.platform or not self.platform.napalm_driver:
            self.logger.debug("no napalm driver defined")
            return None

        self.logger.debug(f"looking for napalm driver '{self.platform.napalm_driver}'")
        try:
            # Driver found, instanciate it
            driver = napalm.get_network_driver(self.platform.napalm_driver)
            self.logger.debug(f"found napalm driver '{self.platform.napalm_driver}'")

            # Merge NAPALM args: first global, then platform's, finish with router's
            args = settings.NAPALM_ARGS
            if self.platform.napalm_args:
                args.update(self.platform.napalm_args)
            if self.napalm_args:
                args.update(self.napalm_args)

            return driver(
                hostname=self.hostname,
                username=self.napalm_username or settings.NAPALM_USERNAME,
                password=self.napalm_password or settings.NAPALM_PASSWORD,
                timeout=self.napalm_timeout or settings.NAPALM_TIMEOUT,
                optional_args=args,
            )
        except napalm.base.exceptions.ModuleImportError:
            # Unable to import proper driver from napalm
            # Most probably due to a broken install
            self.logger.error(
                f"no napalm driver '{self.platform}' found (not installed or does not exist)"
            )
            return None

    def open_napalm_device(self, device):
        """
        Opens a connection with a device using NAPALM.

        This method returns True if the connection is properly opened or False
        in any other cases. It handles exceptions that can occur during the
        connection opening process by itself.

        It is a wrapper method mostly used for logging purpose.
        """
        success = False

        if not device:
            return success

        try:
            self.logger.debug(f"connecting to {self.hostname}")
            device.open()
        except napalm.base.exceptions.ConnectionException as e:
            self.logger.error(
                f'error while trying to connect to {self.hostname} reason "{e}"'
            )
        except Exception:
            self.logger.error(f"error while trying to connect to {self.hostname}")
        else:
            self.logger.debug(f"successfully connected to {self.hostname}")
            success = True
        finally:
            return success

    def close_napalm_device(self, device):
        """
        Closes a connection with a device using NAPALM.

        This method returns True if the connection is properly closed or False
        if the device is not valid.

        It is a wrapper method mostly used for logging purpose.
        """
        if not device:
            return False

        device.close()
        self.logger.debug(f"closing connection with {self.hostname}")

        return True

    def test_napalm_connection(self):
        """
        Opens and closes a connection with a device using NAPALM to see if it
        is possible to interact with it.

        This method returns True only if the connection opening and closing are
        both successful.
        """
        opened, alive, closed = False, False, False
        device = self.get_napalm_device()

        # Open and close the test_napalm_connection
        self.logger.debug(f"testing connection with {self.hostname}")
        opened = self.open_napalm_device(device)
        if opened:
            alive = device.is_alive()
            if alive:
                closed = self.close_napalm_device(device)

        # Issue while opening or closing the connection
        if not opened or not closed or not alive:
            self.logger.error(
                f"cannot connect to {self.hostname}, napalm functions won't work"
            )

        return opened and closed and alive

    def set_napalm_configuration(self, config, commit=False):
        """
        Tries to merge a given configuration on a device using NAPALM.

        This methods returns the changes applied to the configuration if the
        merge was successful. It will return None in any other cases.

        The optional named argument 'commit' is a boolean which is used to
        know if the changes must be commited or discarded. The default value is
        False which means that the changes will be discarded.
        """
        error, changes = None, None

        # Ensure device is enabled, we allow maintenance mode to force a config push
        if self.device_state == DeviceState.DISABLED:
            self.logger.debug(f"device: {self.name} is disabled, exiting config push")
            return "device is disabled, cannot deploy config", changes

        # Make sure there actually a configuration to merge
        if config is None or not isinstance(config, str) or not config.strip():
            self.logger.debug(f"no configuration to merge: {config}")
            error = "no configuration found to be merged"
            return error, changes

        device = self.get_napalm_device()
        opened = self.open_napalm_device(device)

        if opened:
            try:
                # Load the config
                self.logger.debug(f"merging configuration on {self.hostname}")
                device.load_merge_candidate(config=config)
                self.logger.debug(f"merged configuration\n{config}")

                # Get the config diff
                self.logger.debug(
                    f"checking for configuration changes on {self.hostname}"
                )
                changes = device.compare_config()
                self.logger.debug(f"raw napalm output\n{changes}")

                # Commit the config if required
                if commit:
                    self.logger.debug(f"commiting configuration on {self.hostname}")
                    device.commit_config()
                else:
                    self.logger.debug(f"discarding configuration on {self.hostname}")
                    device.discard_config()
            except napalm.base.exceptions.MergeConfigException as e:
                error = f'unable to merge configuration on {self.hostname} reason "{e}"'
                changes = None
                self.logger.debug(error)
            except Exception as e:
                error = f'unable to merge configuration on {self.hostname} reason "{e}"'
                changes = None
                self.logger.debug(error)
            else:
                self.logger.debug(
                    f"successfully merged configuration on {self.hostname}"
                )
            finally:
                closed = self.close_napalm_device(device)
                if not closed:
                    self.logger.debug(
                        f"error while closing connection with {self.hostname}"
                    )
        else:
            error = f"unable to connect to {self.hostname}"

        return error, changes

    def _napalm_bgp_neighbors_to_peer_list(self, napalm_dict):
        bgp_peers = []

        if not napalm_dict:
            return bgp_peers

        # For each VRF
        for vrf in napalm_dict:
            # Get peers inside it
            peers = napalm_dict[vrf]["peers"]
            self.logger.debug(
                "found %s bgp neighbors in %s vrf on %s", len(peers), vrf, self.hostname
            )

            # For each peer handle its IP address and the needed details
            for ip, details in peers.items():
                if "remote_as" not in details:
                    self.logger.debug(
                        "ignored bgp neighbor %s in %s vrf on %s",
                        ip,
                        vrf,
                        self.hostname,
                    )
                elif ip in [str(i["ip_address"]) for i in bgp_peers]:
                    self.logger.debug(
                        "duplicate bgp neighbor %s on %s", ip, self.hostname
                    )
                else:
                    try:
                        # Save the BGP session (IP and remote ASN)
                        bgp_peers.append(
                            {
                                "ip_address": ipaddress.ip_address(ip),
                                "remote_asn": details["remote_as"],
                            }
                        )
                    except ValueError as e:
                        # Error while parsing the IP address
                        self.logger.error(
                            'ignored bgp neighbor %s in %s vrf on %s reason "%s"',
                            ip,
                            vrf,
                            self.hostname,
                            e,
                        )
                        # Force next iteration
                        continue

        return bgp_peers

    def get_napalm_bgp_neighbors(self):
        """
        Returns a list of dictionaries listing all BGP neighbors found on the
        router using NAPALM.

        Each dictionary contains two keys 'ip_address' and 'remote_asn'.

        If an error occurs or no BGP neighbors can be found, the returned list
        will be empty.
        """
        bgp_sessions = []

        device = self.get_napalm_device()
        opened = self.open_napalm_device(device)

        if opened:
            # Get all BGP neighbors on the router
            self.logger.debug("getting bgp neighbors on %s", self.hostname)
            bgp_neighbors = device.get_bgp_neighbors()
            self.logger.debug("raw napalm output %s", bgp_neighbors)
            self.logger.debug(
                "found %s vrfs with bgp neighbors on %s",
                len(bgp_neighbors),
                self.hostname,
            )

            bgp_sessions = self._napalm_bgp_neighbors_to_peer_list(bgp_neighbors)
            self.logger.debug(
                "found %s bgp neighbors on %s", len(bgp_sessions), self.hostname
            )

            # Close connection to the device
            closed = self.close_napalm_device(device)
            if not closed:
                self.logger.debug(
                    "error while closing connection with %s", self.hostname
                )

        return bgp_sessions

    def get_netbox_bgp_neighbors(self):
        """
        Returns a list of dictionaries listing all BGP neighbors found on the
        router using NetBox.

        Each dictionary contains two keys 'ip_address' and 'remote_asn'.

        If an error occurs or no BGP neighbors can be found, the returned list
        will be empty.
        """
        bgp_sessions = []

        self.logger.debug("getting bgp neighbors on %s", self.hostname)
        bgp_neighbors = NetBox().napalm(self.netbox_device_id, "get_bgp_neighbors")
        self.logger.debug("raw napalm output %s", bgp_neighbors)
        self.logger.debug(
            "found %s vrfs with bgp neighbors on %s", len(bgp_neighbors), self.hostname
        )

        bgp_sessions = self._napalm_bgp_neighbors_to_peer_list(bgp_neighbors)
        self.logger.debug(
            "found %s bgp neighbors on %s", len(bgp_sessions), self.hostname
        )

        return bgp_sessions

    def get_bgp_neighbors(self):
        """
        Returns a list of dictionaries listing all BGP neighbors found on the
        router using either NAPALM or NetBox based on the use_netbox flag.

        Each dictionary contains two keys 'ip_address' and 'remote_asn'.

        If an error occurs or no BGP neighbors can be found, the returned list
        will be empty.
        """
        if self.use_netbox:
            return self.get_netbox_bgp_neighbors()
        else:
            return self.get_napalm_bgp_neighbors()

    def find_bgp_neighbor_detail(self, bgp_neighbors, ip_address):
        """
        Finds and returns a single BGP neighbor amongst others.
        """
        # NAPALM dict expected
        if not isinstance(bgp_neighbors, dict):
            return None

        # Make sure to use an IP object
        if isinstance(ip_address, str):
            ip_address = ipaddress.ip_address(ip_address)

        for _, asn in bgp_neighbors.items():
            for _, neighbors in asn.items():
                for neighbor in neighbors:
                    neighbor_ip_address = ipaddress.ip_address(
                        neighbor["remote_address"]
                    )
                    if ip_address == neighbor_ip_address:
                        return neighbor

        return None

    def get_napalm_bgp_neighbors_detail(self, ip_address=None):
        """
        Returns a list of dictionaries listing all BGP neighbors found on the
        router using NAPALM and there respective detail.

        If an error occurs or no BGP neighbors can be found, the returned list
        will be empty.
        """
        bgp_neighbors_detail = []

        device = self.get_napalm_device()
        opened = self.open_napalm_device(device)

        if opened:
            # Get all BGP neighbors on the router
            self.logger.debug("getting bgp neighbors detail on %s", self.hostname)
            bgp_neighbors_detail = device.get_bgp_neighbors_detail()
            self.logger.debug("raw napalm output %s", bgp_neighbors_detail)
            self.logger.debug(
                "found %s vrfs with bgp neighbors on %s",
                len(bgp_neighbors_detail),
                self.hostname,
            )

            # Close connection to the device
            closed = self.close_napalm_device(device)
            if not closed:
                self.logger.debug(
                    "error while closing connection with %s", self.hostname
                )

        return (
            bgp_neighbors_detail
            if not ip_address
            else self.find_bgp_neighbor_detail(bgp_neighbors_detail, ip_address)
        )

    def get_netbox_bgp_neighbors_detail(self, ip_address=None):
        """
        Returns a list of dictionaries listing all BGP neighbors found on the
        router using NetBox and their respective detail.

        If an error occurs or no BGP neighbors can be found, the returned list
        will be empty.
        """
        bgp_neighbors_detail = []

        self.logger.debug("getting bgp neighbors detail on %s", self.hostname)
        bgp_neighbors_detail = NetBox().napalm(
            self.netbox_device_id, "get_bgp_neighbors_detail"
        )
        self.logger.debug("raw napalm output %s", bgp_neighbors_detail)
        self.logger.debug(
            "found %s vrfs with bgp neighbors on %s",
            len(bgp_neighbors_detail),
            self.hostname,
        )

        return (
            bgp_neighbors_detail
            if not ip_address
            else self.find_bgp_neighbor_detail(bgp_neighbors_detail, ip_address)
        )

    def get_bgp_neighbors_detail(self, ip_address=None):
        """
        Returns a list of dictionaries listing all BGP neighbors found on the router
        using either NAPALM or NetBox depending on the use_netbox flag and their
        respective detail.

        If the `ip_address` named parameter is not `None`, only the neighbor with this
        IP address will be returned

        If an error occurs or no BGP neighbors can be found, the returned list
        will be empty.
        """
        if self.use_netbox:
            return self.get_netbox_bgp_neighbors_detail(ip_address=ip_address)
        else:
            return self.get_napalm_bgp_neighbors_detail(ip_address=ip_address)

    def bgp_neighbors_detail_as_list(self, bgp_neighbors_detail):
        """
        Returns a list based on the dict returned by calling
        get_napalm_bgp_neighbors_detail.
        """
        flattened = []

        if not bgp_neighbors_detail:
            return flattened

        for vrf in bgp_neighbors_detail:
            for asn in bgp_neighbors_detail[vrf]:
                flattened.extend(bgp_neighbors_detail[vrf][asn])

        return flattened


class RoutingPolicy(ChangeLoggedModel, TaggableModel):
    name = models.CharField(max_length=128)
    slug = models.SlugField(unique=True, max_length=255)
    type = models.CharField(
        max_length=50,
        choices=RoutingPolicyType.choices,
        default=RoutingPolicyType.IMPORT,
    )
    weight = models.PositiveSmallIntegerField(
        default=0, help_text="The higher the number, the higher the priority"
    )
    address_family = models.PositiveSmallIntegerField(
        default=IPFamily.ALL, choices=IPFamily.choices
    )
    config_context = models.JSONField(blank=True, null=True)
    comments = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "routing policies"
        ordering = ["-weight", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("peering:routingpolicy_details", args=[self.pk])

    def get_type_html(self, display_name=False):
        if self.type == RoutingPolicyType.EXPORT:
            badge_type = "badge-primary"
            text = self.get_type_display()
        elif self.type == RoutingPolicyType.IMPORT:
            badge_type = "badge-info"
            text = self.get_type_display()
        elif self.type == RoutingPolicyType.IMPORT_EXPORT:
            badge_type = "badge-dark"
            text = self.get_type_display()
        else:
            badge_type = "badge-secondary"
            text = "Unknown"

        if display_name:
            text = self.name

        return mark_safe(f'<span class="badge {badge_type}">{text}</span>')
