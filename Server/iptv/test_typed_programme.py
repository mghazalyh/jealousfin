"""Typed programme evidence and current-session publication regressions."""
import dataclasses
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import episode_audio as audio
import test_episode_audio as fixtures


class TypedEvidenceTests(unittest.TestCase):
    def context(self, **kwargs):
        return dict(channelID=fixtures.CHANNEL, seriesTitle="", **kwargs)

    def movie(self, kind="movie", title="Example Film", year=1975):
        terms=("amity", "great white shark", "marine biologist")
        row=audio.TypedProgrammeEvidence(kind,title,"https://www.nbc.com/movie/example",terms,release_year=year)
        reports=[dataclasses.replace(row,source_url=url,published_at="2025-06-20")
                 for url in ("https://www.theguardian.com/film/example","https://www.tvline.com/film/example")]
        request=fixtures.nxt_request(clues=terms)
        return request,row,reports

    def sport(self):
        terms=("indianapolis", "alex palou", "marcus ericsson")
        row=audio.TypedProgrammeEvidence("sports-event","2025 Indianapolis 500","https://www.nbc.com/sports/example",terms,event_date="2025-05-25")
        reports=[dataclasses.replace(row,source_url=url,published_at="2025-05-26")
                 for url in ("https://www.theguardian.com/sport/example","https://www.tvline.com/sport/example")]
        return fixtures.nxt_request(clues=terms),row,reports

    def verify(self, request,row,reports):
        return audio.verify_episode_content(request,[row],reports,fixtures.NOW,self.context())

    def test_movie_identity_and_no_fake_episode_fields(self):
        request,row,reports=self.movie()
        verification=self.verify(request,row,reports)
        self.assertTrue(verification.accepted,verification.reason)
        self.assertEqual(verification.identity.evidence(),{"contentKind":"movie","contentTitle":"Example Film","contentReleaseYear":1975})
        promotion=audio.promote_episode(request,[row],reports,fixtures.current(),fixtures.NOW,programme=self.context())
        self.assertTrue(promotion.accepted)
        self.assertEqual(promotion.episode.evidence()["source"],"local-audio-programme-identity")
        self.assertNotIn("originalAirDate",promotion.episode.evidence())
        store=audio.PublicationStore(clock=lambda:fixtures.NOW)
        self.assertFalse(store.publish(promotion,self.context(),fixtures.NOW))
        ctx=self.context(publicationSessionHash="current-session")
        self.assertTrue(store.publish(promotion,ctx,fixtures.NOW))
        result=store.get(fixtures.CHANNEL,fixtures.NOW)
        self.assertEqual(result["contentCorroboratingSources"],2)
        self.assertFalse(any(k.startswith("episode") for k in result))
        store.discard_publication(promotion,ctx,fixtures.NOW)
        self.assertIsNone(store.get(fixtures.CHANNEL,fixtures.NOW))

    def test_movie_requires_same_release_year_two_publishers_and_shared_plot_clues(self):
        request,row,reports=self.movie()
        self.assertFalse(self.verify(request,row,reports[:1]).accepted)
        self.assertFalse(self.verify(request,row,[reports[0],dataclasses.replace(reports[0],source_url="https://theguardian.com/film/another")]).accepted)
        self.assertFalse(self.verify(request,row,[reports[0],dataclasses.replace(reports[1],release_year=1978)]).accepted)
        self.assertFalse(self.verify(request,dataclasses.replace(row,terms=("amity",)),reports).accepted)
        self.assertFalse(self.verify(request,row,[reports[0],dataclasses.replace(reports[1],terms=("amity",))]).accepted)

    def test_sports_date_and_event_clues_not_publication_date(self):
        request,row,reports=self.sport()
        verdict=self.verify(request,row,reports)
        self.assertTrue(verdict.accepted,verdict.reason)
        self.assertEqual(verdict.identity.evidence()["contentEventDate"],"2025-05-25")
        self.assertNotIn("contentReleaseYear",verdict.identity.evidence())
        self.assertFalse(self.verify(request,dataclasses.replace(row,event_date="2025-05-26"),reports).accepted)
        self.assertFalse(self.verify(request,row,[reports[0],dataclasses.replace(reports[1],published_at="2025-05-24")]).accepted)
        self.assertFalse(self.verify(request,dataclasses.replace(row,title="Race coverage"),[dataclasses.replace(r,title="Race coverage") for r in reports]).accepted)

    def test_series_and_other_are_not_episodes(self):
        for kind in ("series","other-programme"):
            request,row,reports=self.movie(kind)
            v=self.verify(request,row,reports)
            self.assertTrue(v.accepted,v.reason)
            self.assertEqual(v.identity.content_kind,kind)
            self.assertIsNone(v.identity.season_number)

    def test_distinct_work_ambiguity_fails_closed(self):
        request,row,reports=self.movie()
        other=dataclasses.replace(row,title="Example Sequel")
        all_reports=reports+[dataclasses.replace(r,title=other.title) for r in reports]
        result=audio.verify_episode_content(request,[row,other],all_reports,fixtures.NOW,self.context())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason,"ambiguous-content")

    def test_known_metadata_never_replaced(self):
        request,row,reports=self.movie()
        result=audio.verify_episode_content(request,[row],reports,fixtures.NOW,{"seriesTitle":"Existing Film"})
        self.assertFalse(result.accepted)

class TypedGatewayTests(fixtures.GatewayAudioIntegrationTests):
    def test_current_session_typed_row_and_display(self):
        now=time.time()
        terms=("amity","great white shark","marine biologist")
        row=audio.TypedProgrammeEvidence("movie","Example Film","https://www.nbc.com/movie/example",terms,release_year=1975)
        reports=[dataclasses.replace(row,source_url=url,published_at="2025-06-20") for url in
                 ("https://www.theguardian.com/film/example","https://www.tvline.com/film/example")]
        request=fixtures.nxt_request(now,clues=terms)
        ctx={"channelID":fixtures.CHANNEL,"seriesTitle":"","publicationSessionHash":"s1"}
        promotion=audio.promote_episode(request,[row],reports,fixtures.current(now),now,programme=ctx)
        self.assertTrue(promotion.accepted,promotion.reason)
        self.assertTrue(self.g.episode_publisher.publish(promotion,ctx,now))
        with patch.object(self.g.playback_activity,"views",return_value={fixtures.CHANNEL:SimpleNamespace(session_hash="s1")}):
            merged=self.g.merge_episode_publication({},now)
            result=merged[fixtures.CHANNEL]
            self.assertEqual(result["source"],"local-audio-identity")
            self.assertEqual(result["observedAt"],result["contentObservedAt"])
            self.assertTrue(audio.programme_has_resolved_identity(audio.programme_context(result,session_hash="s1"),"s1"))
            self.assertIsNone(audio.programme_context(result,session_hash="s2"))
            self.assertFalse(audio.programme_has_resolved_identity(result))
            self.assertFalse(audio.programme_has_resolved_identity(result,"s1",now+audio.PUBLICATION_TTL+1))
            self.assertFalse(audio.programme_has_episode_identity(result))
            item=self.g.programme_item(fixtures.CHANNEL,result)
            self.assertEqual(item["Name"],"Example Film (1975)")
            self.assertEqual(item["ProductionYear"],1975)
            self.assertNotIn("StartDate",item)
            self.assertNotIn("EndDate",item)
            self.assertNotIn("episodeTitle",item["JealousfinProgramme"])
            self.assertNotIn("audioPlaybackSessionHash",item["JealousfinProgramme"])
            for key in ("contentTitle","contentReleaseYear","contentVerification","contentVisitDigest","contentObservedAt","contentExpiresAt"):
                corrupt=dict(result);corrupt.pop(key)
                self.assertFalse(audio.programme_has_resolved_identity(corrupt,"s1"))
                self.assertIsNone(audio.programme_context(corrupt,session_hash="s1"))
                self.assertIsNone(self.g.programme_item(fixtures.CHANNEL,corrupt))
            for overrides in ({"contentConfidence":float("nan")},{"contentObservedAt":10**1000},
                              {"contentExpiresAt":now+999999},{"contentTitle":"Wrong title"},
                              {"contentReleaseYear":True},{"contentReleaseYear":2100},
                              {"seasonNumber":1,"episodeNumber":2}):
                self.assertFalse(audio.programme_has_resolved_identity(dict(result,**overrides),"s1"))
            existing={fixtures.CHANNEL:{"title":"Example Film","source":"provider-catalogue"}}
            self.assertEqual(self.g.merge_episode_publication(existing.copy(),now),existing)
        with patch.object(self.g.playback_activity,"views",return_value={fixtures.CHANNEL:SimpleNamespace(session_hash="s2")}):
            self.assertEqual(self.g.merge_episode_publication({},now),{})
            self.assertIsNone(self.g.programme_item(fixtures.CHANNEL,result))

    def test_incomplete_typed_identity_cannot_suppress_research(self):
        row={"title":"Unverified","contentKind":"movie","contentSource":"local-audio-programme-identity","contentCorroboratingSources":2}
        self.assertFalse(audio.programme_has_resolved_identity(row,"s1"))
        self.assertIsNone(audio.programme_context(row,session_hash="s1"))

class TypedParserTests(unittest.TestCase):
    def setUp(self):
        self.clues=("martin brody","matt hooper","amity","quint")
        self.official="https://amblin.com/movie/example/"
        self.guardian="https://www.theguardian.com/film/example"
        self.scream="https://www.screamhorrormag.com/example-film-review/"
        self.plot="Martin Brody and Matt Hooper investigate Amity with Quint."
        self.year="1975, when Example opened wide."
        self.movie_html=(
            '<meta property="og:type" content="video.movie">'
            '<meta property="og:title" content="Example (1975) - About the Movie | Amblin">'
            '<meta property="og:url" content="'+self.official+'">'
            '<div class="description" data-description="Synopsis"><p>'+self.plot+'</p></div>')
        self.guardian_html=(
            '<meta property="og:title" content="Example, 40 years on: A film retrospective">'
            '<meta property="article:published_time" content="2015-05-31T09:34:04Z">'
            '<article><h1>Example, 40 years on: A film retrospective</h1>'
            '<p>'+self.year+'</p><p>'+self.plot+'</p></article>')
        self.scream_html=(
            '<meta property="og:title" content="Example: Film Review">'
            '<meta property="article:published_time" content="2015-11-09T12:00:00Z">'
            '<div class="category-film-reviews"><h1>Example: Film Review</h1>'
            '<p>In forty years since its release, Example has been celebrated. Since it’s US release in the summer of 1975, critics praised it.</p>'
            '<p>'+self.plot+'</p></div>')

    def test_three_scoped_movie_profiles(self):
        official=audio.parse_typed_programme_page(self.movie_html,self.official,self.clues,True)
        reports=audio.parse_typed_programme_page(self.guardian_html,self.guardian,self.clues)+audio.parse_typed_programme_page(self.scream_html,self.scream,self.clues)
        self.assertEqual(len(official),1)
        self.assertEqual(len(reports),2)
        v=audio.corroborate_typed_programme(self.clues,official,reports)
        self.assertTrue(v.accepted,v.reason)
        self.assertEqual(v.identity.release_year,1975)

    def test_hidden_or_outside_plot_and_wrong_role_fail(self):
        for body in ('<p hidden>'+self.plot+'</p>','<!--<p>'+self.plot+'</p>-->','<nav><p>'+self.plot+'</p></nav>','<p>(Martin Brody) (Matt Hooper)</p>'):
            bad=self.movie_html.replace('<p>'+self.plot+'</p>',body)
            self.assertEqual(audio.parse_typed_programme_page(bad,self.official,self.clues,True),[])
        self.assertEqual(audio.parse_typed_programme_page(self.movie_html,self.official,self.clues,False),[])
        self.assertEqual(audio.parse_typed_programme_page(self.guardian_html.replace(self.year,'The article was published in 2015.'),self.guardian,self.clues),[])
        self.assertEqual(audio.parse_typed_programme_page(self.guardian_html.replace(self.year,'1975, when Another Film opened wide. Example was mentioned.'),self.guardian,self.clues),[])
        self.assertEqual(audio.parse_typed_programme_page(self.scream_html.replace('Since it’s US release in the summer of 1975','Since Another Film’s US release in 1975'),self.scream,self.clues),[])
        self.assertEqual(audio.parse_typed_programme_page(self.movie_html+self.movie_html,self.official,self.clues,True),[])

    def test_typed_hosts_only_unknown_search_lane(self):
        import opencode_discovery as od
        raw={"official":[self.official],"reports":[self.scream]}
        self.assertEqual(od.normalize_episode_sources(raw),{"official":[],"reports":[]})
        self.assertEqual(od.normalize_episode_sources(raw,include_typed=True),raw)

    def test_indy_event_scopes_and_date_agreement(self):
        clues=("indianapolis","alex palou","david malukas")
        official_url="https://www.indycar.com/news/archive"
        apex_url="https://www.theapex.racing/2025/05/2025-indianapolis-500/"
        racing_url="https://racingnews.co/2025/05/25/indy-500-results/"
        headline="Fantastic Five of 2025: 109th Indianapolis 500"
        official='<meta property="og:title" content="'+headline+'"><section class="section-article"><h1>'+headline+'</h1><li>Race: 109th Indianapolis 500 presented by Gainbridge</li><li>Date: Sunday, May 25</li><li>Winner: Alex Palou</li><p>David Malukas finished the Indianapolis 500.</p></section>'
        pub='<meta property="article:published_time" content="2025-05-26">'
        drivers=["Alex Palou","David Malukas","Third Driver","Fourth Driver","Fifth Driver"]
        cells=''.join('<tr>'+''.join('<td>'+v+'</td>' for v in (str(i),str(i),"10",name,"Team"))+'</tr>' for i,name in enumerate(drivers,1))
        apex=pub+'<meta property="og:title" content="Race Report: 2025 Indianapolis 500 • The Apex"><article class="category-race-report"><h1>2025 Indianapolis 500</h1><h2>Indianapolis Motor Speedway May 25, 2025</h2><div class="rtbs_content" id="2025-indianapolis-500-tab-0"><h3>Race Top Five</h3><table><tr>'+''.join('<th>'+v+'</th>' for v in ("Rank","Start","Car No.","Driver","Team"))+'</tr>'+cells+'</table></div></article>'
        title="Indy 500 Results: May 25, 2025 (Indycar)"
        racing=pub+'<meta property="og:title" content="'+title+'"><h1>'+title+'</h1><div class="dynamic-entry-content"><h2>Indianapolis 500 results</h2><h4>Indy 500 Race Results May 25, 2025 Indycar Series</h4><p>Pos | Driver</p>'+''.join('<p>'+str(i)+'. '+name+'</p>' for i,name in enumerate(drivers,1))+'</div>'
        o=audio.parse_typed_programme_page(official,official_url,clues,True)
        a=audio.parse_typed_programme_page(apex,apex_url,clues)
        r=audio.parse_typed_programme_page(racing,racing_url,clues)
        self.assertEqual([len(o),len(a),len(r)],[1,1,1])
        v=audio.corroborate_typed_programme(clues,o,a+r)
        self.assertTrue(v.accepted,v.reason)
        self.assertEqual(v.identity.event_date,"2025-05-25")
        self.assertEqual(audio.parse_typed_programme_page(official.replace("Date: Sunday, May 25","Schedule: Sunday, May 25"),official_url,clues,True),[])
        self.assertEqual(audio.parse_typed_programme_page(apex.replace("Race Top Five","Starting Grid"),apex_url,clues),[])
        self.assertEqual(audio.parse_typed_programme_page(racing.replace("Indy 500 Race Results May 25","Indy 500 Race Results May 24"),racing_url,clues),[])

    def test_css_hidden_plot_is_not_evidence(self):
        visible='<p>'+self.plot+'</p>'
        for style,markup in (
            ('<style>.concealed {display:none}</style>','<p class="concealed">'+self.plot+'</p>'),
            ('<style>@media(max-width:600px){#concealed{visibility:hidden!important}}</style>','<p id="concealed">'+self.plot+'</p>'),
            ('<style>.description .concealed {opacity:0}</style>','<div class="concealed"><p>'+self.plot+'</p></div>'),
            ('<style>[data-concealed="yes"]{content-visibility:hidden}</style>','<p data-concealed="yes">'+self.plot+'</p>'),
            ('','<p class="screen-reader-text">'+self.plot+'</p>'),
            ('','<p class="d-none">'+self.plot+'</p>'),
            ('','<p style="display:/**/none">'+self.plot+'</p>'),
            ('','<p style="font-size:0.0px">'+self.plot+'</p>'),
            ('','<p style="font-size:0pt">'+self.plot+'</p>'),
            ('','<p style="font-size:-.0rem!important">'+self.plot+'</p>'),
            ('','<p style="font-size:0e3vw">'+self.plot+'</p>'),
            ('','<p style="opacity:0%">'+self.plot+'</p>'),
            ('','<p style="opacity:+.0">'+self.plot+'</p>'),
            ('','<p style="clip-path:inset(100%)">'+self.plot+'</p>'),
            ('','<p style="transform:scale(0)">'+self.plot+'</p>'),
            ('','<p style="-webkit-mask-image:linear-gradient(transparent,transparent)">'+self.plot+'</p>'),
            ('','<p style="filter:opacity(0)">'+self.plot+'</p>'),
            ('','<p style="font-size:calc(0px)">'+self.plot+'</p>'),
            ('','<p style="display:none ! important">'+self.plot+'</p>'),
            ('','<p style="max-height:0;overflow:hidden">'+self.plot+'</p>'),
            ('','<p style="color:transparent">'+self.plot+'</p>'),
            ('','<p aria-hidden=" true ">'+self.plot+'</p>'),
            ('<style>.concealed {transform:scale(0)}</style>','<div class="concealed"><p>'+self.plot+'</p></div>'),

            ('<style>.concealed{font-size:00.000pt}</style>','<p class="concealed">'+self.plot+'</p>'),

            ('','<p style="visib/**/ility: hidden">'+self.plot+'</p>'),
            ('','<p style="display: n'+chr(92)+'6f ne">'+self.plot+'</p>'),
            ('<style>.concealed{display:/**/none}</style>','<p class="concealed">'+self.plot+'</p>'),
        ):
            with self.subTest(markup=markup):
                self.assertEqual(audio.parse_typed_programme_page(style+self.movie_html.replace(visible,markup),self.official,self.clues,True),[])
        # A CSS-hidden unrelated node must not suppress a legitimate visible plot.
        html='<style>.concealed{display:none}</style>'+self.movie_html.replace(visible,'<p class="concealed">Unrelated clues</p>'+visible)
        self.assertEqual(len(audio.parse_typed_programme_page(html,self.official,self.clues,True)),1)

    def test_closed_disclosure_and_dialog_are_not_plot_evidence(self):
        visible='<p>'+self.plot+'</p>'
        for wrapper in ('<details><summary>More</summary>'+visible+'</details>',
                        '<dialog>'+visible+'</dialog>',
                        '<details><summary>More</summary><details open>'+visible+'</details></details>'):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(audio.parse_typed_programme_page(self.movie_html.replace(visible,wrapper),self.official,self.clues,True),[])
        for tag in ('details','dialog'):
            self.assertEqual(len(audio.parse_typed_programme_page(self.movie_html.replace(visible,'<'+tag+' open>'+visible+'</'+tag+'>'),self.official,self.clues,True)),1)
